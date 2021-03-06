#!/usr/bin/python
"""
Copyright (C) 2013 Konstantin Andrusenko
    See the documentation for further information on copyrights,
    or contact the author. All Rights Reserved.

@package id_client.web.web_server
@author Konstantin Andrusenko
@date April 18, 2013

This module contains the implementation of HTTPServer class
"""
import os
import shutil
import time
import signal
import sys
import threading
import logging

from wsgidav.version import __version__
from wsgidav.wsgidav_app import DEFAULT_CONFIG, WsgiDAVApp
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.util import BASE_LOGGER_NAME
from cherrypy import wsgiserver, __version__ as cp_version

from id_client.utils import logger
from id_client.web.web_logic import WSGIApplication, HANDLERS_MAP


class MgmtServer(threading.Thread):
    def __init__(self, host, port, idespositbox_client, static_path=None):
        threading.Thread.__init__(self)
        self.host = host
        self.port = int(port)
        self.idespositbox_client = idespositbox_client
        if not static_path:
            static_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))
        self.static_path = static_path

        self.server = None

    def is_ready(self):
        if not self.server:
            return False
        return self.server.ready

    def run(self):
        web_app = WSGIApplication(self.idespositbox_client, HANDLERS_MAP, self.static_path)

        version = "iDepositBox %s" % wsgiserver.CherryPyWSGIServer.version
        wsgiserver.CherryPyWSGIServer.version = version

        logger.info("Running %s, listening on %s://%s:%s" % (version, 'http', self.host, self.port))

        try:
            self.server = wsgiserver.CherryPyWSGIServer((self.host, self.port), web_app,)

            self.server.start()
        except Exception, err:
            logger.error('Management server error: %s'%err)
            logger.traceback_info()            

    def stop(self):
        try:
            self.idespositbox_client.stop()
            logger.info('Stopping management server...')
            if self.server:
                self.server.stop()
        except Exception, err:
            logger.error('Stopping management server error: %s'%err)
            logger.traceback_info()            

