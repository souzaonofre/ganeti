#
#

# Copyright (C) 2007, 2008 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.

"""HTTP module.

"""

import BaseHTTPServer
import cgi
import logging
import mimetools
import OpenSSL
import os
import select
import socket
import time
import signal
import errno
import threading

from cStringIO import StringIO

from ganeti import constants
from ganeti import serializer
from ganeti import workerpool
from ganeti import utils


HTTP_CLIENT_THREADS = 10

HTTP_GANETI_VERSION = "Ganeti %s" % constants.RELEASE_VERSION

WEEKDAYNAME = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHNAME = [None,
             'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Default error message
DEFAULT_ERROR_CONTENT_TYPE = "text/html"
DEFAULT_ERROR_MESSAGE = """\
<head>
<title>Error response</title>
</head>
<body>
<h1>Error response</h1>
<p>Error code %(code)d.
<p>Message: %(message)s.
<p>Error code explanation: %(code)s = %(explain)s.
</body>
"""

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_NOT_MODIFIED = 304

HTTP_0_9 = "HTTP/0.9"
HTTP_1_0 = "HTTP/1.0"
HTTP_1_1 = "HTTP/1.1"

HTTP_GET = "GET"
HTTP_HEAD = "HEAD"
HTTP_POST = "POST"
HTTP_PUT = "PUT"

HTTP_ETAG = "ETag"
HTTP_HOST = "Host"
HTTP_SERVER = "Server"
HTTP_DATE = "Date"
HTTP_USER_AGENT = "User-Agent"
HTTP_CONTENT_TYPE = "Content-Type"
HTTP_CONTENT_LENGTH = "Content-Length"
HTTP_CONNECTION = "Connection"
HTTP_KEEP_ALIVE = "Keep-Alive"

_SSL_UNEXPECTED_EOF = "Unexpected EOF"

# Socket operations
(SOCKOP_SEND,
 SOCKOP_RECV,
 SOCKOP_SHUTDOWN) = range(3)


class SocketClosed(socket.error):
  pass


class HttpError(Exception):
  """Internal exception for HTTP errors.

  This should only be used for internal error reporting.

  """


class _HttpClientError(Exception):
  """Internal exception for HTTP client errors.

  This should only be used for internal error reporting.

  """


class HttpSocketTimeout(Exception):
  """Internal exception for socket timeouts.

  This should only be used for internal error reporting.

  """


class HttpException(Exception):
  code = None
  message = None

  def __init__(self, message=None):
    Exception.__init__(self)
    if message is not None:
      self.message = message


class HttpBadRequest(HttpException):
  code = 400


class HttpForbidden(HttpException):
  code = 403


class HttpNotFound(HttpException):
  code = 404


class HttpGone(HttpException):
  code = 410


class HttpLengthRequired(HttpException):
  code = 411


class HttpInternalError(HttpException):
  code = 500


class HttpNotImplemented(HttpException):
  code = 501


class HttpServiceUnavailable(HttpException):
  code = 503


class HttpVersionNotSupported(HttpException):
  code = 505


class HttpJsonConverter:
  CONTENT_TYPE = "application/json"

  def Encode(self, data):
    return serializer.DumpJson(data)

  def Decode(self, data):
    return serializer.LoadJson(data)


def WaitForSocketCondition(poller, sock, event, timeout):
  """Waits for a condition to occur on the socket.

  @type poller: select.Poller
  @param poller: Poller object as created by select.poll()
  @type sock: socket
  @param socket: Wait for events on this socket
  @type event: int
  @param event: ORed condition (see select module)
  @type timeout: float or None
  @param timeout: Timeout in seconds
  @rtype: int or None
  @return: None for timeout, otherwise occured conditions

  """
  check = (event | select.POLLPRI |
           select.POLLNVAL | select.POLLHUP | select.POLLERR)

  if timeout is not None:
    # Poller object expects milliseconds
    timeout *= 1000

  poller.register(sock, event)
  try:
    while True:
      # TODO: If the main thread receives a signal and we have no timeout, we
      # could wait forever. This should check a global "quit" flag or
      # something every so often.
      io_events = poller.poll(timeout)
      if not io_events:
        # Timeout
        return None
      for (evfd, evcond) in io_events:
        if evcond & check:
          return evcond
  finally:
    poller.unregister(sock)


def SocketOperation(poller, sock, op, arg1, timeout):
  """Wrapper around socket functions.

  This function abstracts error handling for socket operations, especially
  for the complicated interaction with OpenSSL.

  @type poller: select.Poller
  @param poller: Poller object as created by select.poll()
  @type sock: socket
  @param socket: Socket for the operation
  @type op: int
  @param op: Operation to execute (SOCKOP_* constants)
  @type arg1: any
  @param arg1: Parameter for function (if needed)
  @type timeout: None or float
  @param timeout: Timeout in seconds or None

  """
  # TODO: event_poll/event_check/override
  if op == SOCKOP_SEND:
    event_poll = select.POLLOUT
    event_check = select.POLLOUT

  elif op == SOCKOP_RECV:
    event_poll = select.POLLIN
    event_check = select.POLLIN | select.POLLPRI

  elif op == SOCKOP_SHUTDOWN:
    event_poll = None
    event_check = None

    # The timeout is only used when OpenSSL requests polling for a condition.
    # It is not advisable to have no timeout for shutdown.
    assert timeout

  else:
    raise AssertionError("Invalid socket operation")

  # No override by default
  event_override = 0

  while True:
    # Poll only for certain operations and when asked for by an override
    if event_override or op in (SOCKOP_SEND, SOCKOP_RECV):
      if event_override:
        wait_for_event = event_override
      else:
        wait_for_event = event_poll

      event = WaitForSocketCondition(poller, sock, wait_for_event, timeout)
      if event is None:
        raise HttpSocketTimeout()

      if (op == SOCKOP_RECV and
          event & (select.POLLNVAL | select.POLLHUP | select.POLLERR)):
        return ""

      if not event & wait_for_event:
        continue

    # Reset override
    event_override = 0

    try:
      try:
        if op == SOCKOP_SEND:
          return sock.send(arg1)

        elif op == SOCKOP_RECV:
          return sock.recv(arg1)

        elif op == SOCKOP_SHUTDOWN:
          if isinstance(sock, OpenSSL.SSL.ConnectionType):
            # PyOpenSSL's shutdown() doesn't take arguments
            return sock.shutdown()
          else:
            return sock.shutdown(arg1)

      except OpenSSL.SSL.WantWriteError:
        # OpenSSL wants to write, poll for POLLOUT
        event_override = select.POLLOUT
        continue

      except OpenSSL.SSL.WantReadError:
        # OpenSSL wants to read, poll for POLLIN
        event_override = select.POLLIN | select.POLLPRI
        continue

      except OpenSSL.SSL.WantX509LookupError:
        continue

      except OpenSSL.SSL.SysCallError, err:
        if op == SOCKOP_SEND:
          # arg1 is the data when writing
          if err.args and err.args[0] == -1 and arg1 == "":
            # errors when writing empty strings are expected
            # and can be ignored
            return 0

        elif op == SOCKOP_RECV:
          if err.args == (-1, _SSL_UNEXPECTED_EOF):
            return ""

        raise socket.error(err.args)

      except OpenSSL.SSL.Error, err:
        raise socket.error(err.args)

    except socket.error, err:
      if err.args and err.args[0] == errno.EAGAIN:
        # Ignore EAGAIN
        continue

      raise


def ShutdownConnection(poller, sock, close_timeout, write_timeout, msgreader,
                       force):
  """Closes the connection.

  """
  poller = select.poll()

  #print msgreader.peer_will_close, force
  if msgreader and msgreader.peer_will_close and not force:
    # Wait for peer to close
    try:
      # Check whether it's actually closed
      if not SocketOperation(poller, sock, SOCKOP_RECV, 1, close_timeout):
        return
    except (socket.error, HttpError, HttpSocketTimeout):
      # Ignore errors at this stage
      pass

  # Close the connection from our side
  try:
    SocketOperation(poller, sock, SOCKOP_SHUTDOWN, socket.SHUT_RDWR,
                    write_timeout)
  except HttpSocketTimeout:
    raise HttpError("Timeout while shutting down connection")
  except socket.error, err:
    raise HttpError("Error while shutting down connection: %s" % err)


class HttpSslParams(object):
  """Data class for SSL key and certificate.

  """
  def __init__(self, ssl_key_path, ssl_cert_path):
    """Initializes this class.

    @type ssl_key_path: string
    @param ssl_key_path: Path to file containing SSL key in PEM format
    @type ssl_cert_path: string
    @param ssl_cert_path: Path to file containing SSL certificate in PEM format

    """
    self.ssl_key_pem = utils.ReadFile(ssl_key_path)
    self.ssl_cert_pem = utils.ReadFile(ssl_cert_path)

  def GetKey(self):
    return OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                          self.ssl_key_pem)

  def GetCertificate(self):
    return OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                           self.ssl_cert_pem)


class HttpSocketBase(object):
  """Base class for HTTP server and client.

  """
  def __init__(self):
    self._using_ssl = None
    self._ssl_params = None
    self._ssl_key = None
    self._ssl_cert = None

  def _CreateSocket(self, ssl_params, ssl_verify_peer):
    """Creates a TCP socket and initializes SSL if needed.

    @type ssl_params: HttpSslParams
    @param ssl_params: SSL key and certificate
    @type ssl_verify_peer: bool
    @param ssl_verify_peer: Whether to require client certificate and compare
                            it with our certificate

    """
    self._ssl_params = ssl_params

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Should we enable SSL?
    self._using_ssl = ssl_params is not None

    if not self._using_ssl:
      return sock

    self._ssl_key = ssl_params.GetKey()
    self._ssl_cert = ssl_params.GetCertificate()

    ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
    ctx.set_options(OpenSSL.SSL.OP_NO_SSLv2)

    ctx.use_privatekey(self._ssl_key)
    ctx.use_certificate(self._ssl_cert)
    ctx.check_privatekey()

    if ssl_verify_peer:
      ctx.set_verify(OpenSSL.SSL.VERIFY_PEER |
                     OpenSSL.SSL.VERIFY_FAIL_IF_NO_PEER_CERT,
                     self._SSLVerifyCallback)

    return OpenSSL.SSL.Connection(ctx, sock)

  def _SSLVerifyCallback(self, conn, cert, errnum, errdepth, ok):
    """Verify the certificate provided by the peer

    We only compare fingerprints. The client must use the same certificate as
    we do on our side.

    """
    assert self._ssl_params, "SSL not initialized"

    return (self._ssl_cert.digest("sha1") == cert.digest("sha1") and
            self._ssl_cert.digest("md5") == cert.digest("md5"))


class HttpServerRequestExecutor(object):
  """Implements server side of HTTP

  This class implements the server side of HTTP. It's based on code of Python's
  BaseHTTPServer, from both version 2.4 and 3k. It does not support non-ASCII
  character encodings. Keep-alive connections are not supported.

  """
  # The default request version.  This only affects responses up until
  # the point where the request line is parsed, so it mainly decides what
  # the client gets back when sending a malformed request line.
  # Most web servers default to HTTP 0.9, i.e. don't send a status line.
  default_request_version = HTTP_0_9

  # Error message settings
  error_message_format = DEFAULT_ERROR_MESSAGE
  error_content_type = DEFAULT_ERROR_CONTENT_TYPE

  responses = BaseHTTPServer.BaseHTTPRequestHandler.responses

  def __init__(self, server, conn, client_addr, fileio_class):
    """Initializes this class.

    Part of the initialization is reading the request and eventual POST/PUT
    data sent by the client.

    """
    self._server = server

    # We default rfile to buffered because otherwise it could be
    # really slow for large data (a getc() call per byte); we make
    # wfile unbuffered because (a) often after a write() we want to
    # read and we need to flush the line; (b) big writes to unbuffered
    # files are typically optimized by stdio even when big reads
    # aren't.
    self.rfile = fileio_class(conn, mode="rb", bufsize=-1)
    self.wfile = fileio_class(conn, mode="wb", bufsize=0)

    self.client_addr = client_addr

    self.request_headers = None
    self.request_method = None
    self.request_path = None
    self.request_requestline = None
    self.request_version = self.default_request_version

    self.response_body = None
    self.response_code = HTTP_OK
    self.response_content_type = None
    self.response_headers = {}

    logging.info("Connection from %s:%s", client_addr[0], client_addr[1])
    try:
      try:
        try:
          try:
            # Read, parse and handle request
            self._ReadRequest()
            self._ReadPostData()
            self._HandleRequest()
          except HttpException, err:
            self._SetErrorStatus(err)
        finally:
          # Try to send a response
          self._SendResponse()
          self._Close()
      except SocketClosed:
        pass
    finally:
      logging.info("Disconnected %s:%s", client_addr[0], client_addr[1])

  def _Close(self):
    if not self.wfile.closed:
      self.wfile.flush()
    self.wfile.close()
    self.rfile.close()

  def _DateTimeHeader(self):
    """Return the current date and time formatted for a message header.

    """
    (year, month, day, hh, mm, ss, wd, _, _) = time.gmtime()
    return ("%s, %02d %3s %4d %02d:%02d:%02d GMT" %
            (WEEKDAYNAME[wd], day, MONTHNAME[month], year, hh, mm, ss))

  def _SetErrorStatus(self, err):
    """Sets the response code and body from a HttpException.

    @type err: HttpException
    @param err: Exception instance

    """
    try:
      (shortmsg, longmsg) = self.responses[err.code]
    except KeyError:
      shortmsg = longmsg = "Unknown"

    if err.message:
      message = err.message
    else:
      message = shortmsg

    values = {
      "code": err.code,
      "message": cgi.escape(message),
      "explain": longmsg,
      }

    self.response_code = err.code
    self.response_content_type = self.error_content_type
    self.response_body = self.error_message_format % values

  def _HandleRequest(self):
    """Handle the actual request.

    Calls the actual handler function and converts exceptions into HTTP errors.

    """
    # Don't do anything if there's already been a problem
    if self.response_code != HTTP_OK:
      return

    assert self.request_method, "Status code %s requires a method" % HTTP_OK

    # Check whether client is still there
    self.rfile.read(0)

    try:
      try:
        result = self._server.HandleRequest(self)

        # TODO: Content-type
        encoder = HttpJsonConverter()
        body = encoder.Encode(result)

        self.response_content_type = encoder.CONTENT_TYPE
        self.response_body = body
      except (HttpException, KeyboardInterrupt, SystemExit):
        raise
      except Exception, err:
        logging.exception("Caught exception")
        raise HttpInternalError(message=str(err))
      except:
        logging.exception("Unknown exception")
        raise HttpInternalError(message="Unknown error")

    except HttpException, err:
      self._SetErrorStatus(err)

  def _SendResponse(self):
    """Sends response to the client.

    """
    # Check whether client is still there
    self.rfile.read(0)

    logging.info("%s:%s %s %s", self.client_addr[0], self.client_addr[1],
                 self.request_requestline, self.response_code)

    if self.response_code in self.responses:
      response_message = self.responses[self.response_code][0]
    else:
      response_message = ""

    if self.request_version != HTTP_0_9:
      self.wfile.write("%s %d %s\r\n" %
                       (self.request_version, self.response_code,
                        response_message))
      self._SendHeader(HTTP_SERVER, HTTP_GANETI_VERSION)
      self._SendHeader(HTTP_DATE, self._DateTimeHeader())
      self._SendHeader(HTTP_CONTENT_TYPE, self.response_content_type)
      self._SendHeader(HTTP_CONTENT_LENGTH, str(len(self.response_body)))
      for key, val in self.response_headers.iteritems():
        self._SendHeader(key, val)

      # We don't support keep-alive at this time
      self._SendHeader(HTTP_CONNECTION, "close")
      self.wfile.write("\r\n")

    if (self.request_method != HTTP_HEAD and
        self.response_code >= HTTP_OK and
        self.response_code not in (HTTP_NO_CONTENT, HTTP_NOT_MODIFIED)):
      self.wfile.write(self.response_body)

  def _SendHeader(self, name, value):
    if self.request_version != HTTP_0_9:
      self.wfile.write("%s: %s\r\n" % (name, value))

  def _ReadRequest(self):
    """Reads and parses request line

    """
    raw_requestline = self.rfile.readline()

    requestline = raw_requestline
    if requestline[-2:] == '\r\n':
      requestline = requestline[:-2]
    elif requestline[-1:] == '\n':
      requestline = requestline[:-1]

    if not requestline:
      raise HttpBadRequest("Empty request line")

    self.request_requestline = requestline

    logging.debug("HTTP request: %s", raw_requestline.rstrip("\r\n"))

    words = requestline.split()

    if len(words) == 3:
      [method, path, version] = words
      if version[:5] != 'HTTP/':
        raise HttpBadRequest("Bad request version (%r)" % version)

      try:
        base_version_number = version.split('/', 1)[1]
        version_number = base_version_number.split(".")

        # RFC 2145 section 3.1 says there can be only one "." and
        #   - major and minor numbers MUST be treated as
        #      separate integers;
        #   - HTTP/2.4 is a lower version than HTTP/2.13, which in
        #      turn is lower than HTTP/12.3;
        #   - Leading zeros MUST be ignored by recipients.
        if len(version_number) != 2:
          raise HttpBadRequest("Bad request version (%r)" % version)

        version_number = int(version_number[0]), int(version_number[1])
      except (ValueError, IndexError):
        raise HttpBadRequest("Bad request version (%r)" % version)

      if version_number >= (2, 0):
        raise HttpVersionNotSupported("Invalid HTTP Version (%s)" %
                                      base_version_number)

    elif len(words) == 2:
      version = HTTP_0_9
      [method, path] = words
      if method != HTTP_GET:
        raise HttpBadRequest("Bad HTTP/0.9 request type (%r)" % method)

    else:
      raise HttpBadRequest("Bad request syntax (%r)" % requestline)

    # Examine the headers and look for a Connection directive
    headers = mimetools.Message(self.rfile, 0)

    self.request_method = method
    self.request_path = path
    self.request_version = version
    self.request_headers = headers

  def _ReadPostData(self):
    """Reads POST/PUT data

    Quoting RFC1945, section 7.2 (HTTP/1.0): "The presence of an entity body in
    a request is signaled by the inclusion of a Content-Length header field in
    the request message headers. HTTP/1.0 requests containing an entity body
    must include a valid Content-Length header field."

    """
    # While not according to specification, we only support an entity body for
    # POST and PUT.
    if (not self.request_method or
        self.request_method.upper() not in (HTTP_POST, HTTP_PUT)):
      self.request_post_data = None
      return

    content_length = None
    try:
      if HTTP_CONTENT_LENGTH in self.request_headers:
        content_length = int(self.request_headers[HTTP_CONTENT_LENGTH])
    except TypeError:
      pass
    except ValueError:
      pass

    # 411 Length Required is specified in RFC2616, section 10.4.12 (HTTP/1.1)
    if content_length is None:
      raise HttpLengthRequired("Missing Content-Length header or"
                               " invalid format")

    data = self.rfile.read(content_length)

    # TODO: Content-type, error handling
    if data:
      self.request_post_data = HttpJsonConverter().Decode(data)
    else:
      self.request_post_data = None

    logging.debug("HTTP POST data: %s", self.request_post_data)


class HttpServer(HttpSocketBase):
  """Generic HTTP server class

  Users of this class must subclass it and override the HandleRequest function.

  """
  MAX_CHILDREN = 20

  def __init__(self, mainloop, local_address, port,
               ssl_params=None, ssl_verify_peer=False):
    """Initializes the HTTP server

    @type mainloop: ganeti.daemon.Mainloop
    @param mainloop: Mainloop used to poll for I/O events
    @type local_addess: string
    @param local_address: Local IP address to bind to
    @type port: int
    @param port: TCP port to listen on
    @type ssl_params: HttpSslParams
    @param ssl_params: SSL key and certificate
    @type ssl_verify_peer: bool
    @param ssl_verify_peer: Whether to require client certificate and compare
                            it with our certificate

    """
    HttpSocketBase.__init__(self)

    self.mainloop = mainloop
    self.local_address = local_address
    self.port = port

    self.socket = self._CreateSocket(ssl_params, ssl_verify_peer)

    # Allow port to be reused
    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    if self._using_ssl:
      self._fileio_class = _SSLFileObject
    else:
      self._fileio_class = socket._fileobject

    self._children = []

    mainloop.RegisterIO(self, self.socket.fileno(), select.POLLIN)
    mainloop.RegisterSignal(self)

  def Start(self):
    self.socket.bind((self.local_address, self.port))
    self.socket.listen(5)

  def Stop(self):
    self.socket.close()

  def OnIO(self, fd, condition):
    if condition & select.POLLIN:
      self._IncomingConnection()

  def OnSignal(self, signum):
    if signum == signal.SIGCHLD:
      self._CollectChildren(True)

  def _CollectChildren(self, quick):
    """Checks whether any child processes are done

    @type quick: bool
    @param quick: Whether to only use non-blocking functions

    """
    if not quick:
      # Don't wait for other processes if it should be a quick check
      while len(self._children) > self.MAX_CHILDREN:
        try:
          # Waiting without a timeout brings us into a potential DoS situation.
          # As soon as too many children run, we'll not respond to new
          # requests. The real solution would be to add a timeout for children
          # and killing them after some time.
          pid, status = os.waitpid(0, 0)
        except os.error:
          pid = None
        if pid and pid in self._children:
          self._children.remove(pid)

    for child in self._children:
      try:
        pid, status = os.waitpid(child, os.WNOHANG)
      except os.error:
        pid = None
      if pid and pid in self._children:
        self._children.remove(pid)

  def _IncomingConnection(self):
    """Called for each incoming connection

    """
    (connection, client_addr) = self.socket.accept()

    self._CollectChildren(False)

    pid = os.fork()
    if pid == 0:
      # Child process
      try:
        HttpServerRequestExecutor(self, connection, client_addr,
                                  self._fileio_class)
      except:
        logging.exception("Error while handling request from %s:%s",
                          client_addr[0], client_addr[1])
        os._exit(1)
      os._exit(0)
    else:
      self._children.append(pid)

  def HandleRequest(self, req):
    raise NotImplementedError()


class HttpClientRequest(object):
  def __init__(self, host, port, method, path, headers=None, post_data=None,
               ssl_params=None, ssl_verify_peer=False):
    """Describes an HTTP request.

    @type host: string
    @param host: Hostname
    @type port: int
    @param port: Port
    @type method: string
    @param method: Method name
    @type path: string
    @param path: Request path
    @type headers: dict or None
    @param headers: Additional headers to send
    @type post_data: string or None
    @param post_data: Additional data to send
    @type ssl_params: HttpSslParams
    @param ssl_params: SSL key and certificate
    @type ssl_verify_peer: bool
    @param ssl_verify_peer: Whether to compare our certificate with server's
                            certificate

    """
    if post_data is not None:
      assert method.upper() in (HTTP_POST, HTTP_PUT), \
        "Only POST and GET requests support sending data"

    assert path.startswith("/"), "Path must start with slash (/)"

    self.host = host
    self.port = port
    self.ssl_params = ssl_params
    self.ssl_verify_peer = ssl_verify_peer
    self.method = method
    self.path = path
    self.headers = headers
    self.post_data = post_data

    self.success = None
    self.error = None

    self.resp_status_line = None
    self.resp_version = None
    self.resp_status = None
    self.resp_reason = None
    self.resp_headers = None
    self.resp_body = None


class HttpClientRequestExecutor(HttpSocketBase):
  # Default headers
  DEFAULT_HEADERS = {
    HTTP_USER_AGENT: HTTP_GANETI_VERSION,
    # TODO: For keep-alive, don't send "Connection: close"
    HTTP_CONNECTION: "close",
    }

  # Length limits
  STATUS_LINE_LENGTH_MAX = 512
  HEADER_LENGTH_MAX = 4 * 1024

  # Timeouts in seconds for socket layer
  # TODO: Make read timeout configurable per OpCode
  CONNECT_TIMEOUT = 5.0
  WRITE_TIMEOUT = 10
  READ_TIMEOUT = None
  CLOSE_TIMEOUT = 1

  # Parser state machine
  PS_STATUS_LINE = "status-line"
  PS_HEADERS = "headers"
  PS_BODY = "body"
  PS_COMPLETE = "complete"

  def __init__(self, req):
    """Initializes the HttpClientRequestExecutor class.

    @type req: HttpClientRequest
    @param req: Request object

    """
    HttpSocketBase.__init__(self)

    self.request = req

    self.parser_status = self.PS_STATUS_LINE
    self.header_buffer = StringIO()
    self.body_buffer = StringIO()
    self.content_length = None
    self.server_will_close = None

    self.poller = select.poll()

    try:
      # TODO: Implement connection caching/keep-alive
      self.sock = self._CreateSocket(req.ssl_params,
                                     req.ssl_verify_peer)

      # Disable Python's timeout
      self.sock.settimeout(None)

      # Operate in non-blocking mode
      self.sock.setblocking(0)

      force_close = True
      self._Connect()
      try:
        self._SendRequest()
        self._ReadResponse()

        # Only wait for server to close if we didn't have any exception.
        force_close = False
      finally:
        self._CloseConnection(force_close)

      self.sock.close()
      self.sock = None

      req.resp_body = self.body_buffer.getvalue()

      req.success = True
      req.error = None

    except _HttpClientError, err:
      req.success = False
      req.error = str(err)

  def _BuildRequest(self):
    """Build HTTP request.

    @rtype: string
    @return: Complete request

    """
    # Headers
    send_headers = self.DEFAULT_HEADERS.copy()

    if self.request.headers:
      send_headers.update(self.request.headers)

    send_headers[HTTP_HOST] = "%s:%s" % (self.request.host, self.request.port)

    if self.request.post_data:
      send_headers[HTTP_CONTENT_LENGTH] = len(self.request.post_data)

    buf = StringIO()

    # Add request line. We only support HTTP/1.0 (no chunked transfers and no
    # keep-alive).
    # TODO: For keep-alive, change to HTTP/1.1
    buf.write("%s %s %s\r\n" % (self.request.method.upper(),
                                self.request.path, HTTP_1_0))

    # Add headers
    for name, value in send_headers.iteritems():
      buf.write("%s: %s\r\n" % (name, value))

    buf.write("\r\n")

    if self.request.post_data:
      buf.write(self.request.post_data)

    return buf.getvalue()

  def _ParseStatusLine(self):
    """Parses the status line sent by the server.

    """
    line = self.request.resp_status_line

    if not line:
      raise _HttpClientError("Empty status line")

    try:
      [version, status, reason] = line.split(None, 2)
    except ValueError:
      try:
        [version, status] = line.split(None, 1)
        reason = ""
      except ValueError:
        version = HTTP_9_0

    if version:
      version = version.upper()

    if version not in (HTTP_1_0, HTTP_1_1):
      # We do not support HTTP/0.9, despite the specification requiring it
      # (RFC2616, section 19.6)
      raise _HttpClientError("Only HTTP/1.0 and HTTP/1.1 are supported (%r)" %
                             line)

    # The status code is a three-digit number
    try:
      status = int(status)
      if status < 100 or status > 999:
        status = -1
    except ValueError:
      status = -1

    if status == -1:
      raise _HttpClientError("Invalid status code (%r)" % line)

    self.request.resp_version = version
    self.request.resp_status = status
    self.request.resp_reason = reason

  def _WillServerCloseConnection(self):
    """Evaluate whether server will close the connection.

    @rtype: bool
    @return: Whether server will close the connection

    """
    hdr_connection = self.request.resp_headers.get(HTTP_CONNECTION, None)
    if hdr_connection:
      hdr_connection = hdr_connection.lower()

    # An HTTP/1.1 server is assumed to stay open unless explicitly closed.
    if self.request.resp_version == HTTP_1_1:
      return (hdr_connection and "close" in hdr_connection)

    # Some HTTP/1.0 implementations have support for persistent connections,
    # using rules different than HTTP/1.1.

    # For older HTTP, Keep-Alive indicates persistent connection.
    if self.request.resp_headers.get(HTTP_KEEP_ALIVE):
      return False

    # At least Akamai returns a "Connection: Keep-Alive" header, which was
    # supposed to be sent by the client.
    if hdr_connection and "keep-alive" in hdr_connection:
      return False

    return True

  def _ParseHeaders(self):
    """Parses the headers sent by the server.

    This function also adjusts internal variables based on the header values.

    """
    req = self.request

    # Parse headers
    self.header_buffer.seek(0, 0)
    req.resp_headers = mimetools.Message(self.header_buffer, 0)

    self.server_will_close = self._WillServerCloseConnection()

    # Do we have a Content-Length header?
    hdr_content_length = req.resp_headers.get(HTTP_CONTENT_LENGTH, None)
    if hdr_content_length:
      try:
        self.content_length = int(hdr_content_length)
      except ValueError:
        pass
      if self.content_length is not None and self.content_length < 0:
        self.content_length = None

    # does the body have a fixed length? (of zero)
    if (req.resp_status in (HTTP_NO_CONTENT, HTTP_NOT_MODIFIED) or
        100 <= req.resp_status < 200 or req.method == HTTP_HEAD):
      self.content_length = 0

    # if the connection remains open and a content-length was not provided,
    # then assume that the connection WILL close.
    if self.content_length is None:
      self.server_will_close = True

  def _CheckStatusLineLength(self, length):
    if length > self.STATUS_LINE_LENGTH_MAX:
      raise _HttpClientError("Status line longer than %d chars" %
                             self.STATUS_LINE_LENGTH_MAX)

  def _CheckHeaderLength(self, length):
    if length > self.HEADER_LENGTH_MAX:
      raise _HttpClientError("Headers longer than %d chars" %
                             self.HEADER_LENGTH_MAX)

  def _ParseBuffer(self, buf, eof):
    """Main function for HTTP response state machine.

    @type buf: string
    @param buf: Receive buffer
    @type eof: bool
    @param eof: Whether we've reached EOF on the socket
    @rtype: string
    @return: Updated receive buffer

    """
    if self.parser_status == self.PS_STATUS_LINE:
      # Expect status line
      idx = buf.find("\r\n")
      if idx >= 0:
        self.request.resp_status_line = buf[:idx]

        self._CheckStatusLineLength(len(self.request.resp_status_line))

        # Remove status line, including CRLF
        buf = buf[idx + 2:]

        self._ParseStatusLine()

        self.parser_status = self.PS_HEADERS
      else:
        # Check whether incoming data is getting too large, otherwise we just
        # fill our read buffer.
        self._CheckStatusLineLength(len(buf))

    if self.parser_status == self.PS_HEADERS:
      # Wait for header end
      idx = buf.find("\r\n\r\n")
      if idx >= 0:
        self.header_buffer.write(buf[:idx + 2])

        self._CheckHeaderLength(self.header_buffer.tell())

        # Remove headers, including CRLF
        buf = buf[idx + 4:]

        self._ParseHeaders()

        self.parser_status = self.PS_BODY
      else:
        # Check whether incoming data is getting too large, otherwise we just
        # fill our read buffer.
        self._CheckHeaderLength(len(buf))

    if self.parser_status == self.PS_BODY:
      self.body_buffer.write(buf)
      buf = ""

      # Check whether we've read everything
      if (eof or
          (self.content_length is not None and
           self.body_buffer.tell() >= self.content_length)):
        self.parser_status = self.PS_COMPLETE

    return buf

  def _Connect(self):
    """Non-blocking connect to host with timeout.

    """
    connected = False
    while True:
      try:
        connect_error = self.sock.connect_ex((self.request.host,
                                              self.request.port))
      except socket.gaierror, err:
        raise _HttpClientError("Connection failed: %s" % str(err))

      if connect_error == errno.EINTR:
        # Mask signals
        pass

      elif connect_error == 0:
        # Connection established
        connected = True
        break

      elif connect_error == errno.EINPROGRESS:
        # Connection started
        break

      raise _HttpClientError("Connection failed (%s: %s)" %
                             (connect_error, os.strerror(connect_error)))

    if not connected:
      # Wait for connection
      event = WaitForSocketCondition(self.poller, self.sock,
                                     select.POLLOUT, self.CONNECT_TIMEOUT)
      if event is None:
        raise _HttpClientError("Timeout while connecting to server")

      # Get error code
      connect_error = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
      if connect_error != 0:
        raise _HttpClientError("Connection failed (%s: %s)" %
                               (connect_error, os.strerror(connect_error)))

    # Enable TCP keep-alive
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    # If needed, Linux specific options are available to change the TCP
    # keep-alive settings, see "man 7 tcp" for TCP_KEEPCNT, TCP_KEEPIDLE and
    # TCP_KEEPINTVL.

  def _SendRequest(self):
    """Sends request to server.

    """
    buf = self._BuildRequest()

    while buf:
      # Send only 4 KB at a time
      data = buf[:4096]

      try:
        sent = SocketOperation(self.poller, self.sock, SOCKOP_SEND, data,
                               self.WRITE_TIMEOUT)
      except HttpSocketTimeout:
        raise _HttpClientError("Timeout while sending request")
      except socket.error, err:
        raise _HttpClientError("Error sending request: %s" % err)

      # Remove sent bytes
      buf = buf[sent:]

    assert not buf, "Request wasn't sent completely"

  def _ReadResponse(self):
    """Read response from server.

    Calls the parser function after reading a chunk of data.

    """
    buf = ""
    eof = False
    while self.parser_status != self.PS_COMPLETE:
      try:
        data = SocketOperation(self.poller, self.sock, SOCKOP_RECV, 4096,
                               self.READ_TIMEOUT)
      except HttpSocketTimeout:
        raise _HttpClientError("Timeout while reading response")
      except socket.error, err:
        raise _HttpClientError("Error while reading response: %s" % err)

      if data:
        buf += data
      else:
        eof = True

      # Do some parsing and error checking while more data arrives
      buf = self._ParseBuffer(buf, eof)

      # Must be done only after the buffer has been evaluated
      if (eof and
          self.parser_status in (self.PS_STATUS_LINE,
                                 self.PS_HEADERS)):
        raise _HttpClientError("Connection closed prematurely")

    # Parse rest
    buf = self._ParseBuffer(buf, True)

    assert self.parser_status == self.PS_COMPLETE
    assert not buf, "Parser didn't read full response"

  def _CloseConnection(self, force):
    """Closes the connection.

    """
    if self.server_will_close and not force:
      # Wait for server to close
      try:
        # Check whether it's actually closed
        if not SocketOperation(self.poller, self.sock, SOCKOP_RECV, 1,
                               self.CLOSE_TIMEOUT):
          return
      except (socket.error, _HttpClientError, HttpSocketTimeout):
        # Ignore errors at this stage
        pass

    # Close the connection from our side
    try:
      SocketOperation(self.poller, self.sock, SOCKOP_SHUTDOWN,
                      socket.SHUT_RDWR, self.WRITE_TIMEOUT)
    except HttpSocketTimeout:
      raise _HttpClientError("Timeout while shutting down connection")
    except socket.error, err:
      raise _HttpClientError("Error while shutting down connection: %s" % err)


class _HttpClientPendingRequest(object):
  """Data class for pending requests.

  """
  def __init__(self, request):
    self.request = request

    # Thread synchronization
    self.done = threading.Event()


class HttpClientWorker(workerpool.BaseWorker):
  """HTTP client worker class.

  """
  def RunTask(self, pend_req):
    try:
      HttpClientRequestExecutor(pend_req.request)
    finally:
      pend_req.done.set()


class HttpClientWorkerPool(workerpool.WorkerPool):
  def __init__(self, manager):
    workerpool.WorkerPool.__init__(self, HTTP_CLIENT_THREADS,
                                   HttpClientWorker)
    self.manager = manager


class HttpClientManager(object):
  """Manages HTTP requests.

  """
  def __init__(self):
    self._wpool = HttpClientWorkerPool(self)

  def __del__(self):
    self.Shutdown()

  def ExecRequests(self, requests):
    """Execute HTTP requests.

    This function can be called from multiple threads at the same time.

    @type requests: List of HttpClientRequest instances
    @param requests: The requests to execute
    @rtype: List of HttpClientRequest instances
    @returns: The list of requests passed in

    """
    # _HttpClientPendingRequest is used for internal thread synchronization
    pending = [_HttpClientPendingRequest(req) for req in requests]

    try:
      # Add requests to queue
      for pend_req in pending:
        self._wpool.AddTask(pend_req)

    finally:
      # In case of an exception we should still wait for the rest, otherwise
      # another thread from the worker pool could modify the request object
      # after we returned.

      # And wait for them to finish
      for pend_req in pending:
        pend_req.done.wait()

    # Return original list
    return requests

  def Shutdown(self):
    self._wpool.Quiesce()
    self._wpool.TerminateWorkers()


class _SSLFileObject(object):
  """Wrapper around socket._fileobject

  This wrapper is required to handle OpenSSL exceptions.

  """
  def _RequireOpenSocket(fn):
    def wrapper(self, *args, **kwargs):
      if self.closed:
        raise SocketClosed("Socket is closed")
      return fn(self, *args, **kwargs)
    return wrapper

  def __init__(self, sock, mode='rb', bufsize=-1):
    self._base = socket._fileobject(sock, mode=mode, bufsize=bufsize)

  def _ConnectionLost(self):
    self._base = None

  def _getclosed(self):
    return self._base is None or self._base.closed
  closed = property(_getclosed, doc="True if the file is closed")

  @_RequireOpenSocket
  def close(self):
    return self._base.close()

  @_RequireOpenSocket
  def flush(self):
    return self._base.flush()

  @_RequireOpenSocket
  def fileno(self):
    return self._base.fileno()

  @_RequireOpenSocket
  def read(self, size=-1):
    return self._ReadWrapper(self._base.read, size=size)

  @_RequireOpenSocket
  def readline(self, size=-1):
    return self._ReadWrapper(self._base.readline, size=size)

  def _ReadWrapper(self, fn, *args, **kwargs):
    while True:
      try:
        return fn(*args, **kwargs)

      except OpenSSL.SSL.ZeroReturnError, err:
        self._ConnectionLost()
        return ""

      except OpenSSL.SSL.WantReadError:
        continue

      #except OpenSSL.SSL.WantWriteError:
      # TODO

      except OpenSSL.SSL.SysCallError, (retval, desc):
        if ((retval == -1 and desc == _SSL_UNEXPECTED_EOF)
            or retval > 0):
          self._ConnectionLost()
          return ""

        logging.exception("Error in OpenSSL")
        self._ConnectionLost()
        raise socket.error(err.args)

      except OpenSSL.SSL.Error, err:
        self._ConnectionLost()
        raise socket.error(err.args)

  @_RequireOpenSocket
  def write(self, data):
    return self._WriteWrapper(self._base.write, data)

  def _WriteWrapper(self, fn, *args, **kwargs):
    while True:
      try:
        return fn(*args, **kwargs)
      except OpenSSL.SSL.ZeroReturnError, err:
        self._ConnectionLost()
        return 0

      except OpenSSL.SSL.WantWriteError:
        continue

      #except OpenSSL.SSL.WantReadError:
      # TODO

      except OpenSSL.SSL.SysCallError, err:
        if err.args[0] == -1 and data == "":
          # errors when writing empty strings are expected
          # and can be ignored
          return 0

        self._ConnectionLost()
        raise socket.error(err.args)

      except OpenSSL.SSL.Error, err:
        self._ConnectionLost()
        raise socket.error(err.args)


class HttpMessage(object):
  """Data structure for HTTP message.

  """
  def __init__(self):
    self.start_line = None
    self.headers = None
    self.body = None
    self.decoded_body = None


class HttpClientToServerStartLine(object):
  """Data structure for HTTP request start line.

  """
  def __init__(self, method, path, version):
    self.method = method
    self.path = path
    self.version = version

  def __str__(self):
    return "%s %s %s" % (self.method, self.path, self.version)


class HttpServerToClientStartLine(object):
  """Data structure for HTTP response start line.

  """
  def __init__(self, version, code, reason):
    self.version = version
    self.code = code
    self.reason = reason

  def __str__(self):
    return "%s %s %s" % (self.version, self.code, self.reason)


class HttpMessageWriter(object):
  """Writes an HTTP message to a socket.

  """
  def __init__(self, sock, msg, write_timeout):
    self._msg = msg

    self._PrepareMessage()

    buf = self._FormatMessage()

    poller = select.poll()
    while buf:
      # Send only 4 KB at a time
      data = buf[:4096]

      sent = SocketOperation(poller, sock, SOCKOP_SEND, data,
                             write_timeout)

      # Remove sent bytes
      buf = buf[sent:]

    assert not buf, "Message wasn't sent completely"

  def _PrepareMessage(self):
    """Prepares the HTTP message by setting mandatory headers.

    """
    # RFC2616, section 4.3: "The presence of a message-body in a request is
    # signaled by the inclusion of a Content-Length or Transfer-Encoding header
    # field in the request's message-headers."
    if self._msg.body:
      self._msg.headers[HTTP_CONTENT_LENGTH] = len(self._msg.body)

  def _FormatMessage(self):
    """Serializes the HTTP message into a string.

    """
    buf = StringIO()

    # Add start line
    buf.write(str(self._msg.start_line))
    buf.write("\r\n")

    # Add headers
    if self._msg.start_line.version != HTTP_0_9:
      for name, value in self._msg.headers.iteritems():
        buf.write("%s: %s\r\n" % (name, value))

    buf.write("\r\n")

    # Add message body if needed
    if self.HasMessageBody():
      buf.write(self._msg.body)

    elif self._msg.body:
      logging.warning("Ignoring message body")

    return buf.getvalue()

  def HasMessageBody(self):
    """Checks whether the HTTP message contains a body.

    Can be overriden by subclasses.

    """
    return bool(self._msg.body)


class HttpMessageReader(object):
  """Reads HTTP message from socket.

  """
  # Length limits
  START_LINE_LENGTH_MAX = None
  HEADER_LENGTH_MAX = None

  # Parser state machine
  PS_START_LINE = "start-line"
  PS_HEADERS = "headers"
  PS_BODY = "entity-body"
  PS_COMPLETE = "complete"

  def __init__(self, sock, msg, read_timeout):
    self.sock = sock
    self.msg = msg

    self.poller = select.poll()
    self.start_line_buffer = None
    self.header_buffer = StringIO()
    self.body_buffer = StringIO()
    self.parser_status = self.PS_START_LINE
    self.content_length = None
    self.peer_will_close = None

    buf = ""
    eof = False
    while self.parser_status != self.PS_COMPLETE:
      data = SocketOperation(self.poller, sock, SOCKOP_RECV, 4096,
                             read_timeout)

      if data:
        buf += data
      else:
        eof = True

      # Do some parsing and error checking while more data arrives
      buf = self._ContinueParsing(buf, eof)

      # Must be done only after the buffer has been evaluated
      # TODO: Connection-length < len(data read) and connection closed
      if (eof and
          self.parser_status in (self.PS_START_LINE,
                                 self.PS_HEADERS)):
        raise HttpError("Connection closed prematurely")

    # Parse rest
    buf = self._ContinueParsing(buf, True)

    assert self.parser_status == self.PS_COMPLETE
    assert not buf, "Parser didn't read full response"

    msg.body = self.body_buffer.getvalue()

    # TODO: Content-type, error handling
    if msg.body:
      msg.decoded_body = HttpJsonConverter().Decode(msg.body)
    else:
      msg.decoded_body = None

    if msg.decoded_body:
      logging.debug("Message body: %s", msg.decoded_body)

  def _ContinueParsing(self, buf, eof):
    """Main function for HTTP message state machine.

    @type buf: string
    @param buf: Receive buffer
    @type eof: bool
    @param eof: Whether we've reached EOF on the socket
    @rtype: string
    @return: Updated receive buffer

    """
    if self.parser_status == self.PS_START_LINE:
      # Expect start line
      while True:
        idx = buf.find("\r\n")

        # RFC2616, section 4.1: "In the interest of robustness, servers SHOULD
        # ignore any empty line(s) received where a Request-Line is expected.
        # In other words, if the server is reading the protocol stream at the
        # beginning of a message and receives a CRLF first, it should ignore
        # the CRLF."
        if idx == 0:
          # TODO: Limit number of CRLFs for safety?
          buf = buf[:2]
          continue

        if idx > 0:
          self.start_line_buffer = buf[:idx]

          self._CheckStartLineLength(len(self.start_line_buffer))

          # Remove status line, including CRLF
          buf = buf[idx + 2:]

          self.msg.start_line = self.ParseStartLine(self.start_line_buffer)

          self.parser_status = self.PS_HEADERS
        else:
          # Check whether incoming data is getting too large, otherwise we just
          # fill our read buffer.
          self._CheckStartLineLength(len(buf))

        break

    # TODO: Handle messages without headers
    if self.parser_status == self.PS_HEADERS:
      # Wait for header end
      idx = buf.find("\r\n\r\n")
      if idx >= 0:
        self.header_buffer.write(buf[:idx + 2])

        self._CheckHeaderLength(self.header_buffer.tell())

        # Remove headers, including CRLF
        buf = buf[idx + 4:]

        self._ParseHeaders()

        self.parser_status = self.PS_BODY
      else:
        # Check whether incoming data is getting too large, otherwise we just
        # fill our read buffer.
        self._CheckHeaderLength(len(buf))

    if self.parser_status == self.PS_BODY:
      # TODO: Implement max size for body_buffer
      self.body_buffer.write(buf)
      buf = ""

      # Check whether we've read everything
      #
      # RFC2616, section 4.4: "When a message-body is included with a message,
      # the transfer-length of that body is determined by one of the following
      # [...] 5. By the server closing the connection. (Closing the connection
      # cannot be used to indicate the end of a request body, since that would
      # leave no possibility for the server to send back a response.)"
      if (eof or
          self.content_length is None or
          (self.content_length is not None and
           self.body_buffer.tell() >= self.content_length)):
        self.parser_status = self.PS_COMPLETE

    return buf

  def _CheckStartLineLength(self, length):
    """Limits the start line buffer size.

    @type length: int
    @param length: Buffer size

    """
    if (self.START_LINE_LENGTH_MAX is not None and
        length > self.START_LINE_LENGTH_MAX):
      raise HttpError("Start line longer than %d chars" %
                       self.START_LINE_LENGTH_MAX)

  def _CheckHeaderLength(self, length):
    """Limits the header buffer size.

    @type length: int
    @param length: Buffer size

    """
    if (self.HEADER_LENGTH_MAX is not None and
        length > self.HEADER_LENGTH_MAX):
      raise HttpError("Headers longer than %d chars" % self.HEADER_LENGTH_MAX)

  def ParseStartLine(self, start_line):
    """Parses the start line of a message.

    Must be overriden by subclass.

    @type start_line: string
    @param start_line: Start line string

    """
    raise NotImplementedError()

  def _WillPeerCloseConnection(self):
    """Evaluate whether peer will close the connection.

    @rtype: bool
    @return: Whether peer will close the connection

    """
    # RFC2616, section 14.10: "HTTP/1.1 defines the "close" connection option
    # for the sender to signal that the connection will be closed after
    # completion of the response. For example,
    #
    #        Connection: close
    #
    # in either the request or the response header fields indicates that the
    # connection SHOULD NOT be considered `persistent' (section 8.1) after the
    # current request/response is complete."

    hdr_connection = self.msg.headers.get(HTTP_CONNECTION, None)
    if hdr_connection:
      hdr_connection = hdr_connection.lower()

    # An HTTP/1.1 server is assumed to stay open unless explicitly closed.
    if self.msg.start_line.version == HTTP_1_1:
      return (hdr_connection and "close" in hdr_connection)

    # Some HTTP/1.0 implementations have support for persistent connections,
    # using rules different than HTTP/1.1.

    # For older HTTP, Keep-Alive indicates persistent connection.
    if self.msg.headers.get(HTTP_KEEP_ALIVE):
      return False

    # At least Akamai returns a "Connection: Keep-Alive" header, which was
    # supposed to be sent by the client.
    if hdr_connection and "keep-alive" in hdr_connection:
      return False

    return True

  def _ParseHeaders(self):
    """Parses the headers.

    This function also adjusts internal variables based on header values.

    RFC2616, section 4.3: "The presence of a message-body in a request is
    signaled by the inclusion of a Content-Length or Transfer-Encoding header
    field in the request's message-headers."

    """
    # Parse headers
    self.header_buffer.seek(0, 0)
    self.msg.headers = mimetools.Message(self.header_buffer, 0)

    self.peer_will_close = self._WillPeerCloseConnection()

    # Do we have a Content-Length header?
    hdr_content_length = self.msg.headers.get(HTTP_CONTENT_LENGTH, None)
    if hdr_content_length:
      try:
        self.content_length = int(hdr_content_length)
      except ValueError:
        self.content_length = None
      if self.content_length is not None and self.content_length < 0:
        self.content_length = None

    # if the connection remains open and a content-length was not provided,
    # then assume that the connection WILL close.
    if self.content_length is None:
      self.peer_will_close = True