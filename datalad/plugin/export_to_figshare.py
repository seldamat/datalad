# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""export a dataset as a TAR/ZIP archive to figshare"""

__docformat__ = 'restructuredtext'

# whatever -- can move to assistance module later
import json
import requests


class FigshareRESTLaison(object):

    API_URL = 'https://api.figshare.com/v2'

    def __init__(self):
        self._token = None

    @property
    def token(self):
        if self._token is None:
            from datalad.downloaders.providers import Providers
            providers = Providers.from_config_files()
            provider = providers.get_provider(self.API_URL)
            credential = provider.credential
            self._token = credential().get('token')
        return self._token

    def __call__(self, m, url, data=None, success=None, binary=False,
                 headers=None, return_json=True):
        """A wrapper around requests calls

        to interpolate deposition_id, do basic checks and conversion
        """
        if '://' not in url:
            url_ = self.API_URL + '/' + url
        else:
            url_ = url

        headers = headers or {}
        if data is not None and not binary:
            data = json.dumps(data)
            headers["Content-Type"] = "application/json"
        headers['Authorization'] = "token %s" % self.token

        r = m(url_, data=data, headers=headers)
        status_code = r.status_code
        if (success is not "donotcheck") and \
                ((success and status_code not in success)
                 or (not success and status_code >= 400)):
            msg = "Got return code %(status_code)s for %(m)s(%(url_)s." \
                  % locals()
            raise RuntimeError("Error status %s" % msg)

        if return_json:
            return r.json() if r.content else {}
        else:
            return r.content

    def put(self, *args, **kwargs):
        return self(requests.put, *args, **kwargs)

    def post(self, *args, **kwargs):
        return self(requests.post, *args, **kwargs)

    def get(self, *args, **kwargs):
        return self(requests.get, *args, **kwargs)

    # def delete(self, *args, **kwargs):
    #     return self(requests.delete, *args, **kwargs)

    def upload_file(self, fname, files_url):
        # In v2 API seems no easy way to "just upload".  Need to initiate,
        # do uploads
        # and finalize
        import os
        from datalad.utils import md5sum
        from datalad.ui import ui
        file_rec = {'md5': md5sum(fname),
                    'name': os.path.basename(fname),
                    'size': os.stat(fname).st_size
                    }
        # Initiate upload
        j = self.post(files_url, file_rec)
        file_endpoint = j['location']
        file_info = self.get(file_endpoint)
        file_upload_info = self.get(file_info['upload_url'])

        pbar = ui.get_progressbar(label=fname,  # fill_text=f.name,
                                  total=file_rec['size'])
        with open(fname, 'rb') as f:
            for part in file_upload_info['parts']:
                udata = dict(file_info, **part)
                if part['status'] == 'PENDING':
                    f.seek(part['startOffset'])
                    data = f.read(part['endOffset'] - part['startOffset'] + 1)
                    url = '{upload_url}/{partNo}'.format(**udata)
                    ok = self.put(url, data=data, binary=True, return_json=False)
                    assert ok == 'OK'
                pbar.update(part['endOffset'], increment=False)
            pbar.finish()

        # complete upload
        jcomplete = self.post(file_endpoint, return_json=False)
        return file_info


"""
## https://docs.figshare.com/#private_article_publish
## swagger API def https://docs.figshare.com/swagger.json

smells like ideally we should find a good swagger client for Python
and use it across such use-cases

- print all API urls

 jq -r '.paths|keys?' figshare-swagger.json

# Get list of files for an article:
curl -X get "https://api.figshare.com/v2/articles/{article_id}/files"

# details for a file:
curl -X get "https://api.figshare.com/v2/articles/{article_id}/files/{file_id}"

# publish am article
curl -X post "https://api.figshare.com/v2/account/articles/{article_id}/publish"
"""
# PLUGIN API
def dlplugin(dataset, filename=None, on_file_error='error',
             annex=True,
             project_id=None,
             article_id=None
             ):
    """Export the content of a dataset as a ZIP archive to figshare

    Very quick and dirty approach.  Ideally figshare should be supported as
    a proper git annex special remote.  Unfortunately, figshare does not support
    having directories, and can store only a flat list of files.  That makes
    it impossible for any sensible publish'ing of complete datasets.

    The only workaround is to publish dataset as a zip-ball, where the entire
    content is wrapped into a .zip archive for which figshare would provide a
    navigator.


    Parameters
    ----------
    filename : str, optional
      File name of the generated ZIP archive. If no file name is given
      the archive will be generated in the current directory and will
      be named: datalad_<dataset_uuid>.zip.
    on_file_error : {'error', 'continue', 'ignore'}, optional
      By default, any issue accessing a file in the dataset while adding
      it to the TAR archive will result in an error and the plugin is
      aborted. Setting this to 'continue' will issue warnings instead
      of failing on error. The value 'ignore' will only inform about
      problem at the 'debug' log level. The latter two can be helpful
      when generating a TAR archive from a dataset where some file content
      is not available locally.
    annex : bool, optional
      If True generated .zip file would be added to annex, and all files
      would get registered in git-annex to be available from such a tarball. Also
      upon upload we will register for that archive to be a possible source for it
      in annex.
    project_id : int, optional
      If given, article (if article_id is not provided) will be created in that
      project
    article_id : int, optional
      Which article to publish to.
    """
    import os
    from datalad.plugin.export_archive import dlplugin as export_archive
    import logging
    lgr = logging.getLogger('datalad.plugin.export_to_figshare')

    from datalad.ui import ui
    from datalad.ui.progressbars import FileReadProgressbar
    from datalad.downloaders.providers import Providers
    from datalad.downloaders.http import HTTPDownloader
    from datalad.api import add_archive_content

    # pleasing wonderful plugin system
    from datalad.plugin.export_to_figshare import FigshareRESTLaison
    from datalad.support.annexrepo import AnnexRepo

    if not isinstance(dataset.repo, AnnexRepo):
        raise ValueError(
            "%s is not an annex repo, so annexification could be done"
            % dataset
        )

    if dataset.repo.is_dirty():
        raise RuntimeError(
            "Paranoid authors of DataLad refuse to proceed in a dirty repository"
        )

    lgr.info("Exporting current tree as an archive since figshare does not support directories")
    archive_out = next(
        export_archive(
            dataset,
            filename=filename,
            archivetype='zip'
        )
    )
    assert archive_out['status'] == 'ok'
    fname = archive_out['path']

    lgr.info("Uploading %s to figshare", fname)
    figshare = FigshareRESTLaison()

    if not article_id:
        # TODO: we could make it interactive (just an idea)
        if False: # ui.is_interactive():
            # or should we just upload to a new article?
            article_id = ui.question(
                "Which of the articles should we upload to.",
                choices=get_article_ids()
            )
        if not article_id:
            raise ValueError("We need an article to upload to.")

    file_info = figshare.upload_file(
        fname,
        files_url='account/articles/%s/files' % article_id
    )

    if annex:
        # I will leave all the complaining etc to the dataset add if path
        # is outside etc
        lgr.info("'Registering' %s within annex", fname)
        repo = dataset.repo
        repo.add(fname, git=False)
        key = repo.get_file_key(fname)
        lgr.info("Adding URL %(download_url)s for it", file_info)
        repo._annex_custom_command([],
            [
                "git", "annex", "registerurl", '-c', 'annex.alwayscommit=false',
                key, file_info['download_url']
            ]
        )

        lgr.info("Registering links back for the content of the archive")
        add_archive_content(
            fname,
            annex=dataset.repo,
            delete_after=True,
            allow_dirty=True
        )

        lgr.info("Removing generated and now registered in annex archive")
        repo.drop(key, key=True)
        repo.remove(fname)  # remove the tarball

        # if annex in {'delete'}:
        #     dataset.repo.remove(fname)
        # else:
        #     # kinda makes little sense I guess.
        #     # Made more sense if export_archive could export an arbitrary treeish
        #     # so we could create a branch where to dump and export to figshare
        #     # (kinda closer to my idea)
        #     dataset.save(fname, message="Added the entire dataset into a zip file")

    else:
        lgr.info("Removing generated tarball")
        os.unlink(fname)

    yield dict(
        status='ok',
        file_info=file_info,
        path=dataset,
        action='export_to_figshare',
        logger=lgr
    )
