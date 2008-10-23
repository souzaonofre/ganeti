#
#

# Copyright (C) 2006, 2007 Google Inc.
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


"""Module dealing with command line parsing"""


import sys
import textwrap
import os.path
import copy
import time
import logging
from cStringIO import StringIO

from ganeti import utils
from ganeti import errors
from ganeti import constants
from ganeti import opcodes
from ganeti import luxi
from ganeti import ssconf

from optparse import (OptionParser, make_option, TitledHelpFormatter,
                      Option, OptionValueError)

__all__ = ["DEBUG_OPT", "NOHDR_OPT", "SEP_OPT", "GenericMain",
           "SubmitOpCode", "GetClient",
           "cli_option", "ikv_option", "keyval_option",
           "GenerateTable", "AskUser",
           "ARGS_NONE", "ARGS_FIXED", "ARGS_ATLEAST", "ARGS_ANY", "ARGS_ONE",
           "USEUNITS_OPT", "FIELDS_OPT", "FORCE_OPT", "SUBMIT_OPT",
           "ListTags", "AddTags", "RemoveTags", "TAG_SRC_OPT",
           "FormatError", "SplitNodeOption", "SubmitOrSend",
           "JobSubmittedException", "FormatTimestamp", "ParseTimespec",
           "ValidateBeParams",
           "ToStderr", "ToStdout",
           ]


def _ExtractTagsObject(opts, args):
  """Extract the tag type object.

  Note that this function will modify its args parameter.

  """
  if not hasattr(opts, "tag_type"):
    raise errors.ProgrammerError("tag_type not passed to _ExtractTagsObject")
  kind = opts.tag_type
  if kind == constants.TAG_CLUSTER:
    retval = kind, kind
  elif kind == constants.TAG_NODE or kind == constants.TAG_INSTANCE:
    if not args:
      raise errors.OpPrereqError("no arguments passed to the command")
    name = args.pop(0)
    retval = kind, name
  else:
    raise errors.ProgrammerError("Unhandled tag type '%s'" % kind)
  return retval


def _ExtendTags(opts, args):
  """Extend the args if a source file has been given.

  This function will extend the tags with the contents of the file
  passed in the 'tags_source' attribute of the opts parameter. A file
  named '-' will be replaced by stdin.

  """
  fname = opts.tags_source
  if fname is None:
    return
  if fname == "-":
    new_fh = sys.stdin
  else:
    new_fh = open(fname, "r")
  new_data = []
  try:
    # we don't use the nice 'new_data = [line.strip() for line in fh]'
    # because of python bug 1633941
    while True:
      line = new_fh.readline()
      if not line:
        break
      new_data.append(line.strip())
  finally:
    new_fh.close()
  args.extend(new_data)


def ListTags(opts, args):
  """List the tags on a given object.

  This is a generic implementation that knows how to deal with all
  three cases of tag objects (cluster, node, instance). The opts
  argument is expected to contain a tag_type field denoting what
  object type we work on.

  """
  kind, name = _ExtractTagsObject(opts, args)
  op = opcodes.OpGetTags(kind=kind, name=name)
  result = SubmitOpCode(op)
  result = list(result)
  result.sort()
  for tag in result:
    print tag


def AddTags(opts, args):
  """Add tags on a given object.

  This is a generic implementation that knows how to deal with all
  three cases of tag objects (cluster, node, instance). The opts
  argument is expected to contain a tag_type field denoting what
  object type we work on.

  """
  kind, name = _ExtractTagsObject(opts, args)
  _ExtendTags(opts, args)
  if not args:
    raise errors.OpPrereqError("No tags to be added")
  op = opcodes.OpAddTags(kind=kind, name=name, tags=args)
  SubmitOpCode(op)


def RemoveTags(opts, args):
  """Remove tags from a given object.

  This is a generic implementation that knows how to deal with all
  three cases of tag objects (cluster, node, instance). The opts
  argument is expected to contain a tag_type field denoting what
  object type we work on.

  """
  kind, name = _ExtractTagsObject(opts, args)
  _ExtendTags(opts, args)
  if not args:
    raise errors.OpPrereqError("No tags to be removed")
  op = opcodes.OpDelTags(kind=kind, name=name, tags=args)
  SubmitOpCode(op)


DEBUG_OPT = make_option("-d", "--debug", default=False,
                        action="store_true",
                        help="Turn debugging on")

NOHDR_OPT = make_option("--no-headers", default=False,
                        action="store_true", dest="no_headers",
                        help="Don't display column headers")

SEP_OPT = make_option("--separator", default=None,
                      action="store", dest="separator",
                      help="Separator between output fields"
                      " (defaults to one space)")

USEUNITS_OPT = make_option("--human-readable", default=False,
                           action="store_true", dest="human_readable",
                           help="Print sizes in human readable format")

FIELDS_OPT = make_option("-o", "--output", dest="output", action="store",
                         type="string", help="Comma separated list of"
                         " output fields",
                         metavar="FIELDS")

FORCE_OPT = make_option("-f", "--force", dest="force", action="store_true",
                        default=False, help="Force the operation")

TAG_SRC_OPT = make_option("--from", dest="tags_source",
                          default=None, help="File with tag names")

SUBMIT_OPT = make_option("--submit", dest="submit_only",
                         default=False, action="store_true",
                         help="Submit the job and return the job ID, but"
                         " don't wait for the job to finish")


def ARGS_FIXED(val):
  """Macro-like function denoting a fixed number of arguments"""
  return -val


def ARGS_ATLEAST(val):
  """Macro-like function denoting a minimum number of arguments"""
  return val


ARGS_NONE = None
ARGS_ONE = ARGS_FIXED(1)
ARGS_ANY = ARGS_ATLEAST(0)


def check_unit(option, opt, value):
  """OptParsers custom converter for units.

  """
  try:
    return utils.ParseUnit(value)
  except errors.UnitParseError, err:
    raise OptionValueError("option %s: %s" % (opt, err))


class CliOption(Option):
  """Custom option class for optparse.

  """
  TYPES = Option.TYPES + ("unit",)
  TYPE_CHECKER = copy.copy(Option.TYPE_CHECKER)
  TYPE_CHECKER["unit"] = check_unit


def _SplitKeyVal(opt, data):
  """Convert a KeyVal string into a dict.

  This function will convert a key=val[,...] string into a dict. Empty
  values will be converted specially: keys which have the prefix 'no_'
  will have the value=False and the prefix stripped, the others will
  have value=True.

  @type opt: string
  @param opt: a string holding the option name for which we process the
      data, used in building error messages
  @type data: string
  @param data: a string of the format key=val,key=val,...
  @rtype: dict
  @return: {key=val, key=val}
  @raises errors.ParameterError: if there are duplicate keys

  """
  NO_PREFIX = "no_"
  UN_PREFIX = "-"
  kv_dict = {}
  for elem in data.split(","):
    if "=" in elem:
      key, val = elem.split("=", 1)
    else:
      if elem.startswith(NO_PREFIX):
        key, val = elem[len(NO_PREFIX):], False
      elif elem.startswith(UN_PREFIX):
        key, val = elem[len(UN_PREFIX):], None
      else:
        key, val = elem, True
    if key in kv_dict:
      raise errors.ParameterError("Duplicate key '%s' in option %s" %
                                  (key, opt))
    kv_dict[key] = val
  return kv_dict


def check_ident_key_val(option, opt, value):
  """Custom parser for the IdentKeyVal option type.

  """
  if ":" not in value:
    retval =  (value, {})
  else:
    ident, rest = value.split(":", 1)
    kv_dict = _SplitKeyVal(opt, rest)
    retval = (ident, kv_dict)
  return retval


class IdentKeyValOption(Option):
  """Custom option class for ident:key=val,key=val options.

  This will store the parsed values as a tuple (ident, {key: val}). As
  such, multiple uses of this option via action=append is possible.

  """
  TYPES = Option.TYPES + ("identkeyval",)
  TYPE_CHECKER = copy.copy(Option.TYPE_CHECKER)
  TYPE_CHECKER["identkeyval"] = check_ident_key_val


def check_key_val(option, opt, value):
  """Custom parser for the KeyVal option type.

  """
  return _SplitKeyVal(opt, value)


class KeyValOption(Option):
  """Custom option class for key=val,key=val options.

  This will store the parsed values as a dict {key: val}.

  """
  TYPES = Option.TYPES + ("keyval",)
  TYPE_CHECKER = copy.copy(Option.TYPE_CHECKER)
  TYPE_CHECKER["keyval"] = check_key_val


# optparse.py sets make_option, so we do it for our own option class, too
cli_option = CliOption
ikv_option = IdentKeyValOption
keyval_option = KeyValOption


def _ParseArgs(argv, commands, aliases):
  """Parses the command line and return the function which must be
  executed together with its arguments

  Arguments:
    argv: the command line

    commands: dictionary with special contents, see the design doc for
    cmdline handling
    aliases: dictionary with command aliases {'alias': 'target, ...}

  """
  if len(argv) == 0:
    binary = "<command>"
  else:
    binary = argv[0].split("/")[-1]

  if len(argv) > 1 and argv[1] == "--version":
    print "%s (ganeti) %s" % (binary, constants.RELEASE_VERSION)
    # Quit right away. That way we don't have to care about this special
    # argument. optparse.py does it the same.
    sys.exit(0)

  if len(argv) < 2 or not (argv[1] in commands or
                           argv[1] in aliases):
    # let's do a nice thing
    sortedcmds = commands.keys()
    sortedcmds.sort()
    print ("Usage: %(bin)s {command} [options...] [argument...]"
           "\n%(bin)s <command> --help to see details, or"
           " man %(bin)s\n" % {"bin": binary})
    # compute the max line length for cmd + usage
    mlen = max([len(" %s" % cmd) for cmd in commands])
    mlen = min(60, mlen) # should not get here...
    # and format a nice command list
    print "Commands:"
    for cmd in sortedcmds:
      cmdstr = " %s" % (cmd,)
      help_text = commands[cmd][4]
      help_lines = textwrap.wrap(help_text, 79-3-mlen)
      print "%-*s - %s" % (mlen, cmdstr, help_lines.pop(0))
      for line in help_lines:
        print "%-*s   %s" % (mlen, "", line)
    print
    return None, None, None

  # get command, unalias it, and look it up in commands
  cmd = argv.pop(1)
  if cmd in aliases:
    if cmd in commands:
      raise errors.ProgrammerError("Alias '%s' overrides an existing"
                                   " command" % cmd)

    if aliases[cmd] not in commands:
      raise errors.ProgrammerError("Alias '%s' maps to non-existing"
                                   " command '%s'" % (cmd, aliases[cmd]))

    cmd = aliases[cmd]

  func, nargs, parser_opts, usage, description = commands[cmd]
  parser = OptionParser(option_list=parser_opts,
                        description=description,
                        formatter=TitledHelpFormatter(),
                        usage="%%prog %s %s" % (cmd, usage))
  parser.disable_interspersed_args()
  options, args = parser.parse_args()
  if nargs is None:
    if len(args) != 0:
      print >> sys.stderr, ("Error: Command %s expects no arguments" % cmd)
      return None, None, None
  elif nargs < 0 and len(args) != -nargs:
    print >> sys.stderr, ("Error: Command %s expects %d argument(s)" %
                         (cmd, -nargs))
    return None, None, None
  elif nargs >= 0 and len(args) < nargs:
    print >> sys.stderr, ("Error: Command %s expects at least %d argument(s)" %
                         (cmd, nargs))
    return None, None, None

  return func, options, args


def SplitNodeOption(value):
  """Splits the value of a --node option.

  """
  if value and ':' in value:
    return value.split(':', 1)
  else:
    return (value, None)


def ValidateBeParams(bep):
  """Parse and check the given beparams.

  The function will update in-place the given dictionary.

  @type bep: dict
  @param bep: input beparams
  @raise errors.ParameterError: if the input values are not OK
  @raise errors.UnitParseError: if the input values are not OK

  """
  if constants.BE_MEMORY in bep:
    bep[constants.BE_MEMORY] = utils.ParseUnit(bep[constants.BE_MEMORY])

  if constants.BE_VCPUS in bep:
    try:
      bep[constants.BE_VCPUS] = int(bep[constants.BE_VCPUS])
    except ValueError:
      raise errors.ParameterError("Invalid number of VCPUs")


def AskUser(text, choices=None):
  """Ask the user a question.

  Args:
    text - the question to ask.

    choices - list with elements tuples (input_char, return_value,
    description); if not given, it will default to: [('y', True,
    'Perform the operation'), ('n', False, 'Do no do the operation')];
    note that the '?' char is reserved for help

  Returns: one of the return values from the choices list; if input is
  not possible (i.e. not running with a tty, we return the last entry
  from the list

  """
  if choices is None:
    choices = [('y', True, 'Perform the operation'),
               ('n', False, 'Do not perform the operation')]
  if not choices or not isinstance(choices, list):
    raise errors.ProgrammerError("Invalid choiches argument to AskUser")
  for entry in choices:
    if not isinstance(entry, tuple) or len(entry) < 3 or entry[0] == '?':
      raise errors.ProgrammerError("Invalid choiches element to AskUser")

  answer = choices[-1][1]
  new_text = []
  for line in text.splitlines():
    new_text.append(textwrap.fill(line, 70, replace_whitespace=False))
  text = "\n".join(new_text)
  try:
    f = file("/dev/tty", "a+")
  except IOError:
    return answer
  try:
    chars = [entry[0] for entry in choices]
    chars[-1] = "[%s]" % chars[-1]
    chars.append('?')
    maps = dict([(entry[0], entry[1]) for entry in choices])
    while True:
      f.write(text)
      f.write('\n')
      f.write("/".join(chars))
      f.write(": ")
      line = f.readline(2).strip().lower()
      if line in maps:
        answer = maps[line]
        break
      elif line == '?':
        for entry in choices:
          f.write(" %s - %s\n" % (entry[0], entry[2]))
        f.write("\n")
        continue
  finally:
    f.close()
  return answer


class JobSubmittedException(Exception):
  """Job was submitted, client should exit.

  This exception has one argument, the ID of the job that was
  submitted. The handler should print this ID.

  This is not an error, just a structured way to exit from clients.

  """


def SendJob(ops, cl=None):
  """Function to submit an opcode without waiting for the results.

  @type ops: list
  @param ops: list of opcodes
  @type cl: luxi.Client
  @param cl: the luxi client to use for communicating with the master;
             if None, a new client will be created

  """
  if cl is None:
    cl = GetClient()

  job_id = cl.SubmitJob(ops)

  return job_id


def PollJob(job_id, cl=None, feedback_fn=None):
  """Function to poll for the result of a job.

  @type job_id: job identified
  @param job_id: the job to poll for results
  @type cl: luxi.Client
  @param cl: the luxi client to use for communicating with the master;
             if None, a new client will be created

  """
  if cl is None:
    cl = GetClient()

  prev_job_info = None
  prev_logmsg_serial = None

  while True:
    result = cl.WaitForJobChange(job_id, ["status"], prev_job_info,
                                 prev_logmsg_serial)
    if not result:
      # job not found, go away!
      raise errors.JobLost("Job with id %s lost" % job_id)

    # Split result, a tuple of (field values, log entries)
    (job_info, log_entries) = result
    (status, ) = job_info

    if log_entries:
      for log_entry in log_entries:
        (serial, timestamp, _, message) = log_entry
        if callable(feedback_fn):
          feedback_fn(log_entry[1:])
        else:
          print "%s %s" % (time.ctime(utils.MergeTime(timestamp)), message)
        prev_logmsg_serial = max(prev_logmsg_serial, serial)

    # TODO: Handle canceled and archived jobs
    elif status in (constants.JOB_STATUS_SUCCESS, constants.JOB_STATUS_ERROR):
      break

    prev_job_info = job_info

  jobs = cl.QueryJobs([job_id], ["status", "opresult"])
  if not jobs:
    raise errors.JobLost("Job with id %s lost" % job_id)

  status, result = jobs[0]
  if status == constants.JOB_STATUS_SUCCESS:
    return result
  else:
    raise errors.OpExecError(result)


def SubmitOpCode(op, cl=None, feedback_fn=None):
  """Legacy function to submit an opcode.

  This is just a simple wrapper over the construction of the processor
  instance. It should be extended to better handle feedback and
  interaction functions.

  """
  if cl is None:
    cl = GetClient()

  job_id = SendJob([op], cl)

  op_results = PollJob(job_id, cl=cl, feedback_fn=feedback_fn)

  return op_results[0]


def SubmitOrSend(op, opts, cl=None, feedback_fn=None):
  """Wrapper around SubmitOpCode or SendJob.

  This function will decide, based on the 'opts' parameter, whether to
  submit and wait for the result of the opcode (and return it), or
  whether to just send the job and print its identifier. It is used in
  order to simplify the implementation of the '--submit' option.

  """
  if opts and opts.submit_only:
    job_id = SendJob([op], cl=cl)
    raise JobSubmittedException(job_id)
  else:
    return SubmitOpCode(op, cl=cl, feedback_fn=feedback_fn)


def GetClient():
  # TODO: Cache object?
  try:
    client = luxi.Client()
  except luxi.NoMasterError:
    master, myself = ssconf.GetMasterAndMyself()
    if master != myself:
      raise errors.OpPrereqError("This is not the master node, please connect"
                                 " to node '%s' and rerun the command" %
                                 master)
    else:
      raise
  return client


def FormatError(err):
  """Return a formatted error message for a given error.

  This function takes an exception instance and returns a tuple
  consisting of two values: first, the recommended exit code, and
  second, a string describing the error message (not
  newline-terminated).

  """
  retcode = 1
  obuf = StringIO()
  msg = str(err)
  if isinstance(err, errors.ConfigurationError):
    txt = "Corrupt configuration file: %s" % msg
    logging.error(txt)
    obuf.write(txt + "\n")
    obuf.write("Aborting.")
    retcode = 2
  elif isinstance(err, errors.HooksAbort):
    obuf.write("Failure: hooks execution failed:\n")
    for node, script, out in err.args[0]:
      if out:
        obuf.write("  node: %s, script: %s, output: %s\n" %
                   (node, script, out))
      else:
        obuf.write("  node: %s, script: %s (no output)\n" %
                   (node, script))
  elif isinstance(err, errors.HooksFailure):
    obuf.write("Failure: hooks general failure: %s" % msg)
  elif isinstance(err, errors.ResolverError):
    this_host = utils.HostInfo.SysName()
    if err.args[0] == this_host:
      msg = "Failure: can't resolve my own hostname ('%s')"
    else:
      msg = "Failure: can't resolve hostname '%s'"
    obuf.write(msg % err.args[0])
  elif isinstance(err, errors.OpPrereqError):
    obuf.write("Failure: prerequisites not met for this"
               " operation:\n%s" % msg)
  elif isinstance(err, errors.OpExecError):
    obuf.write("Failure: command execution error:\n%s" % msg)
  elif isinstance(err, errors.TagError):
    obuf.write("Failure: invalid tag(s) given:\n%s" % msg)
  elif isinstance(err, errors.JobQueueDrainError):
    obuf.write("Failure: the job queue is marked for drain and doesn't"
               " accept new requests\n")
  elif isinstance(err, errors.GenericError):
    obuf.write("Unhandled Ganeti error: %s" % msg)
  elif isinstance(err, luxi.NoMasterError):
    obuf.write("Cannot communicate with the master daemon.\nIs it running"
               " and listening for connections?")
  elif isinstance(err, luxi.TimeoutError):
    obuf.write("Timeout while talking to the master daemon. Error:\n"
               "%s" % msg)
  elif isinstance(err, luxi.ProtocolError):
    obuf.write("Unhandled protocol error while talking to the master daemon:\n"
               "%s" % msg)
  elif isinstance(err, JobSubmittedException):
    obuf.write("JobID: %s\n" % err.args[0])
    retcode = 0
  else:
    obuf.write("Unhandled exception: %s" % msg)
  return retcode, obuf.getvalue().rstrip('\n')


def GenericMain(commands, override=None, aliases=None):
  """Generic main function for all the gnt-* commands.

  Arguments:
    - commands: a dictionary with a special structure, see the design doc
                for command line handling.
    - override: if not None, we expect a dictionary with keys that will
                override command line options; this can be used to pass
                options from the scripts to generic functions
    - aliases: dictionary with command aliases {'alias': 'target, ...}

  """
  # save the program name and the entire command line for later logging
  if sys.argv:
    binary = os.path.basename(sys.argv[0]) or sys.argv[0]
    if len(sys.argv) >= 2:
      binary += " " + sys.argv[1]
      old_cmdline = " ".join(sys.argv[2:])
    else:
      old_cmdline = ""
  else:
    binary = "<unknown program>"
    old_cmdline = ""

  if aliases is None:
    aliases = {}

  func, options, args = _ParseArgs(sys.argv, commands, aliases)
  if func is None: # parse error
    return 1

  if override is not None:
    for key, val in override.iteritems():
      setattr(options, key, val)

  utils.SetupLogging(constants.LOG_COMMANDS, debug=options.debug,
                     stderr_logging=True, program=binary)

  utils.debug = options.debug

  if old_cmdline:
    logging.info("run with arguments '%s'", old_cmdline)
  else:
    logging.info("run with no arguments")

  try:
    result = func(options, args)
  except (errors.GenericError, luxi.ProtocolError,
          JobSubmittedException), err:
    result, err_msg = FormatError(err)
    logging.exception("Error durring command processing")
    ToStderr(err_msg)

  return result


def GenerateTable(headers, fields, separator, data,
                  numfields=None, unitfields=None):
  """Prints a table with headers and different fields.

  Args:
    headers: Dict of header titles or None if no headers should be shown
    fields: List of fields to show
    separator: String used to separate fields or None for spaces
    data: Data to be printed
    numfields: List of fields to be aligned to right
    unitfields: List of fields to be formatted as units

  """
  if numfields is None:
    numfields = []
  if unitfields is None:
    unitfields = []

  format_fields = []
  for field in fields:
    if headers and field not in headers:
      raise errors.ProgrammerError("Missing header description for field '%s'"
                                   % field)
    if separator is not None:
      format_fields.append("%s")
    elif field in numfields:
      format_fields.append("%*s")
    else:
      format_fields.append("%-*s")

  if separator is None:
    mlens = [0 for name in fields]
    format = ' '.join(format_fields)
  else:
    format = separator.replace("%", "%%").join(format_fields)

  for row in data:
    for idx, val in enumerate(row):
      if fields[idx] in unitfields:
        try:
          val = int(val)
        except ValueError:
          pass
        else:
          val = row[idx] = utils.FormatUnit(val)
      val = row[idx] = str(val)
      if separator is None:
        mlens[idx] = max(mlens[idx], len(val))

  result = []
  if headers:
    args = []
    for idx, name in enumerate(fields):
      hdr = headers[name]
      if separator is None:
        mlens[idx] = max(mlens[idx], len(hdr))
        args.append(mlens[idx])
      args.append(hdr)
    result.append(format % tuple(args))

  for line in data:
    args = []
    for idx in xrange(len(fields)):
      if separator is None:
        args.append(mlens[idx])
      args.append(line[idx])
    result.append(format % tuple(args))

  return result


def FormatTimestamp(ts):
  """Formats a given timestamp.

  @type ts: timestamp
  @param ts: a timeval-type timestamp, a tuple of seconds and microseconds

  @rtype: string
  @returns: a string with the formatted timestamp

  """
  if not isinstance (ts, (tuple, list)) or len(ts) != 2:
    return '?'
  sec, usec = ts
  return time.strftime("%F %T", time.localtime(sec)) + ".%06d" % usec


def ParseTimespec(value):
  """Parse a time specification.

  The following suffixed will be recognized:

    - s: seconds
    - m: minutes
    - h: hours
    - d: day
    - w: weeks

  Without any suffix, the value will be taken to be in seconds.

  """
  value = str(value)
  if not value:
    raise errors.OpPrereqError("Empty time specification passed")
  suffix_map = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
    }
  if value[-1] not in suffix_map:
    try:
      value = int(value)
    except ValueError:
      raise errors.OpPrereqError("Invalid time specification '%s'" % value)
  else:
    multiplier = suffix_map[value[-1]]
    value = value[:-1]
    if not value: # no data left after stripping the suffix
      raise errors.OpPrereqError("Invalid time specification (only"
                                 " suffix passed)")
    try:
      value = int(value) * multiplier
    except ValueError:
      raise errors.OpPrereqError("Invalid time specification '%s'" % value)
  return value


def _ToStream(stream, txt, *args):
  """Write a message to a stream, bypassing the logging system

  @type stream: file object
  @param stream: the file to which we should write
  @type txt: str
  @param txt: the message

  """
  if args:
    args = tuple(args)
    stream.write(txt % args)
  else:
    stream.write(txt)
  stream.write('\n')
  stream.flush()


def ToStdout(txt, *args):
  """Write a message to stdout only, bypassing the logging system

  This is just a wrapper over _ToStream.

  @type txt: str
  @param txt: the message

  """
  _ToStream(sys.stdout, txt, *args)


def ToStderr(txt, *args):
  """Write a message to stderr only, bypassing the logging system

  This is just a wrapper over _ToStream.

  @type txt: str
  @param txt: the message

  """
  _ToStream(sys.stderr, txt, *args)
