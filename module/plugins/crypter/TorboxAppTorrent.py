# -*- coding: utf-8 -*-

import fnmatch
import os
import time
import urllib

import pycurl

from module.network.HTTPRequest import BadHeader, FormFile

from ..internal.misc import exists, json, safejoin, uniqify
from ..internal.SimpleCrypter import SimpleCrypter


class TorboxAppTorrent(SimpleCrypter):
    __name__ = "TorboxAppTorrent"
    __type__ = "crypter"
    __version__ = "0.01"
    __status__ = "testing"

    __pattern__ = r"^unmatchable$"
    __config__ = [
        ("activated", "bool", "Activated", True),
        ("folder_per_package", "Default;Yes;No", "Create folder for each package", "Default"),
        ("max_wait", "int", "Reconnect if waiting time is greater than minutes", 10),
        ("include_filter", "str", "File types to include (e.g. *.iso;*.zip, leave empty to select none)", "*.*"),
        ("exclude_filter", "str", "File types to exclude (e.g. *.exe;advertisement.txt, leave empty to select none)", "")
    ]


    __description__ = """Torbox.app torrents decrypter plugin"""
    __license__ = "GPLv3"
    __authors__ = [("GammaC0de", "nitzo2001[AT]yahoo[DOT]com")]

    # See https://api-docs.torbox.app/
    API_URL = "https://api.torbox.app/v1/api/"

    def api_request(self, method, api_key=None, get={}, post={}):
        if api_key is not None:
            self.req.http.c.setopt(
                pycurl.HTTPHEADER, ["Authorization: Bearer " + api_key]
            )
        multipart = any(
            isinstance(x, FormFile)
            for x in post.values()
        )
        try:
            json_data = self.load(self.API_URL + method, get=get, post=post, multipart=multipart)
        except BadHeader as exc:
            json_data = exc.content

        api_data = json.loads(json_data)
        return api_data

    def sleep(self, sec):
        for _i in range(sec):
            if self.pyfile.abort:
                break
            time.sleep(1)

    def exit_error(self, msg):
        if self.tmp_file:
            os.remove(self.tmp_file)

        self.fail(msg)

    def send_request_to_server(self):
        """ Send torrent/magnet to the server """

        if self.pyfile.url.endswith(".torrent"):
            #: torrent URL
            if self.pyfile.url.startswith("http"):
                #: remote URL, download the torrent to tmp directory
                torrent_content = self.load(self.pyfile.url, decode=False)
                torrent_filename = safejoin(self.pyload.tempdir, "tmp_{}.torrent".format(self.pyfile.package().name))
                with open(torrent_filename, "wb") as fp:
                    fp.write(torrent_content)

            else:
                #: URL is a local torrent file (uploaded container)
                torrent_filename = urllib.url2pathname(self.pyfile.url[7:])  #: trim the starting `file://`
                if not exists(torrent_filename):
                    self.fail(self._("Torrent file does not exist"))

            self.tmp_file = torrent_filename

            #: Check if the torrent file path is inside pyLoad's temp directory
            if os.path.abspath(torrent_filename).startswith(self.pyload.tempdir + os.sep):
                #: yes, send the torrent content to the server
                api_data = self.api_request("torrents/createtorrent",
                                            api_key=self.api_key,
                                            post={"file": FormFile(torrent_filename, mimetype="application/octet-stream")})

                if not api_data.get("success", False):
                    error_msg = api_data["detail"]
                    self.exit_error(error_msg)

            else:
                self.exit_error(self._("Illegal URL"))  #: We don't allow files outside pyLoad's config directory

        else:
            #: magnet URL, send it to the server
            api_data = self.api_request("torrents/createtorrent",
                                        api_key=self.api_key,
                                        post={"magnet": self.pyfile.url})

            if not api_data.get("success", False):
                error_msg = api_data["detail"]
                self.exit_error(error_msg)

        torrent_id = api_data["data"]["torrent_id"]
        torrent_hash = api_data["data"]["hash"]
        return torrent_id, torrent_hash


    def wait_for_server_dl(self, torrent_id, torrent_hash):
        """ Show progress while the server does the download """

        exclude_filters = self.config.get("exclude_filter").split(';')
        include_filters = self.config.get("include_filter").split(";")

        api_data = self.api_request("torrents/checkcached",
                                    api_key=self.api_key,
                                    get={
                                        "hash": torrent_hash,
                                        "format": "object",
                                        "bypass_cache": True,
                                    })

        if api_data.get("success", False) and api_data.get("data"):
            self.pyfile.name = api_data["data"][torrent_hash]["name"]
            self.pyfile.size = api_data["data"][torrent_hash]["size"]

        else:
            self.pyfile.set_custom_status("torrent")
            self.pyfile.set_progress(0)
            while True:
                api_data = self.api_request("torrents/mylist",
                                            api_key=self.api_key,
                                            get={
                                                "id": torrent_id,
                                                "bypass_cache": True,
                                            })

                file_size = api_data["data"].get("size")
                if file_size:
                    self.pyfile.size = file_size
                file_name = api_data["data"].get("name")
                if file_name:
                    self.pyfile.name = file_name

                progress = api_data["data"].get("progress", 0) * 100
                self.pyfile.set_progress(progress)
                if api_data["data"].get("download_state") == "completed":
                    break

                self.sleep(5)

            self.pyfile.set_progress(100)

        api_data = self.api_request("torrents/mylist",
                                    api_key=self.api_key,
                                    get={
                                        "id": torrent_id,
                                        "bypass_cache": True
                                    })

        #: Filter and select files for downloading
        excluded_ids = []
        for _filter in exclude_filters:
            excluded_ids.extend([_file["id"] for _file in api_data["data"].get("files", [])
                                 if fnmatch.fnmatch(os.path.basename(_file["short_name"]), _filter)])

        excluded_ids = uniqify(excluded_ids)

        included_ids = []
        for _filter in include_filters:
            included_ids.extend([_file["id"] for _file in api_data["data"].get("files", [])
                                 if fnmatch.fnmatch(os.path.basename(_file["short_name"]), _filter)])

        included_ids = uniqify(included_ids)

        selected_ids = [
            str(_id)
            for _id in sorted(included_ids)
            if _id not in excluded_ids
        ]

        torrent_urls = [
            "%storrents/requestdl?token=%s&torrent_id=%s&file_id=%s&redirect=true" % (
                self.API_URL, self.api_key, torrent_id, _id
            )
            for _id in selected_ids
        ]

        return torrent_urls

    def delete_torrent_from_server(self, torrent_id):
        """ Remove the torrent from the server """

        pass

    def decrypt(self, pyfile):
        self.tmp_file = None
        torrent_id = 0
        if "TorboxApp" not in self.pyload.accountManager.plugins:
            self.fail(self._("This plugin requires an active Torbox.app account"))

        self.account = self.pyload.accountManager.getAccountPlugin("TorboxApp")
        if len(self.account.accounts) == 0:
            self.fail(self._("This plugin requires an active Torbox.app account"))

        self.api_key = self.account.accounts[list(self.account.accounts.keys())[0]]["password"]

        torrent_id, torrent_hash = self.send_request_to_server()
        torrent_urls = self.wait_for_server_dl(torrent_id, torrent_hash)

        self.packages = [(pyfile.package().name, torrent_urls, pyfile.package().name)]
