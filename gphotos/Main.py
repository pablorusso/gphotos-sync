# coding: utf8
import argparse
import os.path
import logging
import sys

from pkg_resources import DistributionNotFound
from datetime import datetime
from appdirs import AppDirs
from .GooglePhotosSync import GooglePhotosSync
from .GoogleAlbumsSync import GoogleAlbumsSync
from .LocalData import LocalData
from .authorize import Authorize
from .restclient import RestClient
import pkg_resources

if os.name != 'nt':
    import fcntl

APP_NAME = "gphotos-sync"
log = logging.getLogger(__name__)


class GooglePhotosSyncMain:
    def __init__(self):
        self.data_store = None
        self.google_photos_client = None
        self.google_photos_sync = None
        self.google_albums_sync = None
        self.trace_file = None

        self.auth = None

    parser = argparse.ArgumentParser(
        description="Google Photos download tool")
    parser.add_argument(
        "root_folder",
        help="root of the local folders to download into")
    parser.add_argument(
        "--flush-index",
        action='store_true',
        help="delete the index db, re-scan everything")
    parser.add_argument(
        "--rescan",
        action='store_true',
        help="rescan entire library, ignoring last scan date. Use this if you "
             "have added photos to the library that "
             "predate the last sync, or you have deleted some of the local "
             "files")
    parser.add_argument(
        "--retry-download",
        action='store_true',
        help="check for the existence of files marked as already downloaded "
             "and re-download any missing ones. Use "
             "this if you have deleted some local files")
    parser.add_argument(
        "--skip-video",
        action='store_true',
        help="skip video types in sync")
    parser.add_argument(
        "--start-date",
        help="Set the earliest date of files to sync"
             "format YYYY-MM-DD",
        default=None)
    parser.add_argument(
        "--end-date",
        help="Set the latest date of files to sync"
             "format YYYY-MM-DD",
        default=None)
    parser.add_argument(
        "--log-level",
        help="Set log level. Options: critical, error, warning, info, debug",
        default='warning')
    parser.add_argument(
        "--db-path",
        help="Specify a pre-existing folder for the index database. "
             "Defaults to the root of the local download folders",
        default=None)
    parser.add_argument(
        "--new-token",
        action='store_true',
        help="Request new token")
    parser.add_argument(
        "--index-only",
        action='store_true',
        help="Only build the index of files in .gphotos.db - no downloads")
    parser.add_argument(
        "--skip-index",
        action='store_true',
        help="Use index from previous run and start download immediately")
    parser.add_argument(
        "--do-delete",
        action='store_true',
        help="""Remove local copies of files that were deleted.
        Must be used with --flush-db since the deleted items must be removed 
        from the index""")
    parser.add_argument(
        "--skip-files",
        action='store_true',
        help="Dont download files, just refresh the album links(for testing)")
    parser.add_argument(
        "--skip-albums",
        action='store_true',
        help="Dont download albums (for testing)")

    def setup(self, args, db_path):
        app_dirs = AppDirs(APP_NAME)

        self.data_store = LocalData(db_path, args.flush_index)

        credentials_file = os.path.join(db_path, ".gphotos.token")
        secret_file = os.path.join(
            app_dirs.user_config_dir, "client_secret.json")
        if args.new_token and os.path.exists(credentials_file):
            os.remove(credentials_file)

        scope = [
            'https://www.googleapis.com/auth/photoslibrary.readonly',
            'https://www.googleapis.com/auth/photoslibrary.sharing',
        ]
        photos_api_url = 'https://photoslibrary.googleapis.com/$discovery' \
                         '/rest?version=v1'

        self.auth = Authorize(scope, credentials_file, secret_file)
        self.auth.authorize()

        self.google_photos_client = RestClient(photos_api_url,
                                               self.auth.session)
        self.google_photos_sync = GooglePhotosSync(self.google_photos_client,
                                                   args.root_folder,
                                                   self.data_store)
        self.google_albums_sync = GoogleAlbumsSync(self.google_photos_client,
                                                   args.root_folder,
                                                   self.data_store)

        self.google_photos_sync.set_start_date(args.start_date)
        self.google_photos_sync.set_end_date(args.end_date)
        self.google_photos_sync.include_video = not args.skip_video
        self.google_photos_sync.rescan = args.rescan
        self.google_photos_sync.retry_download = args.retry_download

    @classmethod
    def logging(cls, args, folder):
        # if we are debugging requests library is too noisy
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("requests_oauthlib").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        numeric_level = getattr(logging, args.log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % args.log_level)

        log_file = os.path.join(folder, 'gphotos.log')
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s %(name)-12s %(levelname)-8s '
                                   '%(message)s',
                            datefmt='%m-%d %H:%M:%S',
                            filename=log_file,
                            filemode='w')
        # define a Handler which writes INFO messages or higher to the
        # sys.stderr
        console = logging.StreamHandler()
        console.setLevel(numeric_level)
        # set a format which is simpler for console use
        formatter = logging.Formatter('%(asctime)s %(message)s',
                                      datefmt='%m-%d %H:%M:%S')
        # tell the handler to use this format
        console.setFormatter(formatter)
        # add the handler to the root logger
        logging.getLogger('').addHandler(console)

    def start(self, args):
        with self.data_store:
            if not args.skip_index:
                if not args.skip_files:
                    self.google_photos_sync.index_photos_media()
                if not args.skip_albums:
                    self.google_albums_sync.index_album_media()
            if not args.index_only:
                if not args.skip_files:
                    self.google_photos_sync.download_photo_media()
                if not args.skip_albums:
                    self.google_albums_sync.create_album_content_links()
                if args.do_delete:
                    self.google_photos_sync.check_for_removed()

    def main(self, test_args=None):
        start_time = datetime.now()
        args = self.parser.parse_args(test_args)

        db_path = args.db_path if args.db_path else args.root_folder
        args.root_folder = os.path.abspath(args.root_folder)
        if not os.path.exists(args.root_folder):
            os.makedirs(args.root_folder, 0o700)
        self.logging(args, db_path)

        lock_file = os.path.join(db_path, 'gphotos.lock')
        fp = open(lock_file, 'w')
        with fp:
            try:
                if os.name != 'nt':
                    fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except IOError:
                log.warning('EXITING: database is locked')
                sys.exit(0)

            try:
                log.info('version: {}'.format(
                    pkg_resources.get_distribution("gphotos-sync").version))
            except TypeError:
                log.info('version not available')
            except DistributionNotFound:
                log.warning('running under unit tests?')

            # configure and launch
            # noinspection PyBroadException
            try:
                self.setup(args, db_path)
                self.start(args)
            except KeyboardInterrupt:
                log.error("User cancelled download")
                log.debug("Traceback", exc_info=True)
            except BaseException:
                log.error("\nProcess failed.", exc_info=True)
            finally:
                log.warning("Done.")

        elapsed_time = datetime.now() - start_time
        log.info('Elapsed time = %s', elapsed_time)
