#! /usr/bin/env python3

# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2013 Hard Consulting Corporation"
__license__                     = "GNU General Public License, Version 3 (or later)"


"""
enip		-- An server recognizing an Ethernet/IP protocol subset

USAGE
    python -m cpppo.server.enip

BACKGROUND


"""

__all__				= ['main', 'address']

import argparse
import array
import codecs
import errno
import fnmatch
import json
import logging
from   logging import handlers
import os
import random
import sys
import socket
import threading
from   timeit import default_timer as timer
import time
import traceback
try:
    import reprlib
except ImportError:
    import repr as reprlib

import cpppo
from   cpppo import misc
import cpppo.server
from   cpppo.server import network

from . import parser
from . import logix
from . import device

if __name__ == "__main__":
    logging.basicConfig( **cpppo.log_cfg )
    #logging.getLogger().setLevel( logging.DETAIL )


# Globals

log				= logging.getLogger( "enip.srv" )

# The default cpppo.enip.address
address				= ('0.0.0.0', 44818)

# Maintain a global 'options' cpppo.dotdict() containing all our configuration options, configured
# from incoming parsed command-line options.  This'll be passed (ultimately) to the server and
# web_api Thread Thread target functions, broken out as keyword parameters.  As a result, the second
# (and lower) levels of this dotdict will remain as dotdict objects assigned to keywords determined
# by the top level dict keys.  
options				= cpppo.dotdict()

# The stats for the connections presently open, indexed by <interface>:<port>.   Of particular
# interest is connections['key'].eof, which will terminate the connection if set to 1
connections			= cpppo.dotdict()

# All known tags, their CIP Attribute and desired error code
tags				= cpppo.dotdict()

# Optional modules
try:
    import web
except:
    log.warning( "Failed to import web API module; --web option not available" )




# 
# The Web API, implemented using web.py
# 
# 
def deduce_encoding( available, environ, accept=None ):
    """Deduce acceptable encoding from HTTP Accept: header:

        Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8

    If it remains None (or the supplied one is unrecognized), the
    caller should fail to produce the desired content, and return an
    HTML status code 406 Not Acceptable.

    If no Accept: encoding is supplied in the environ, the default
    (first) encoding in order is used.

    We don't test a supplied 'accept' encoding against the HTTP_ACCEPT
    settings, because certain URLs have a fixed encoding.  For
    example, /some/url/blah.json always wants to return
    "application/json", regardless of whether the browser's Accept:
    header indicates it is acceptable.  We *do* however test the
    supplied 'accept' encoding against the 'available' encodings,
    because these are the only ones known to the caller.

    Otherwise, return the first acceptable encoding in 'available'.  If no
    matching encodings are avaliable, return the (original) None.
    """
    if accept:
        # A desired encoding; make sure it is available
        accept		= accept.lower()
        if accept not in available:
            accept	= None
        return accept

    # No predefined accept encoding; deduce preferred available one.  Accept:
    # may contain */*, */json, etc.  If multiple matches, select the one with
    # the highest Accept: quality value (our present None starts with a quality
    # metric of 0.0).  Test available: ["application/json", "text/html"],
    # vs. HTTP_ACCEPT
    # "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" Since
    # earlier matches are for the more preferred encodings, later matches must
    # *exceed* the quality metric of the earlier.
    accept		= None # may be "", False, {}, [], ()
    HTTP_ACCEPT		= environ.get( "HTTP_ACCEPT", "*/*" ).lower() if environ else "*/*"
    quality		= 0.0
    for stanza in HTTP_ACCEPT.split( ',' ):
        # application/xml;q=0.9
        q		= 1.0
        for encoding in reversed( stanza.split( ';' )):
            if encoding.startswith( "q=" ):
                q	= float( encoding[2:] )
        for avail in available:
            match	= True
            for a, t in zip( avail.split( '/' ), encoding.split( '/' )):
                if a != t and t != '*':
                    match = False
            if match:
                log.debug( "Found %16s == %-16s;q=%.1f %s %-16s;q=%.1f",
                           avail, encoding, q,
                           '> ' if q > quality else '<=',
                           accept, quality )
                if q > quality:
                    quality	= q
                    accept	= avail
    return accept


def http_exception( framework, status, message ):
    """Return an exception appropriate for the given web framework,
    encoding the HTTP status code and message provided.
    """
    if framework and framework.__name__ == "web":
        if status == 404:
            return framework.NotFound( message )

        if status == 406:
            class NotAcceptable( framework.NotAcceptable ):
                def __init__(self, message):
                    self.message = '; '.join( [self.message, message] )
                    framework.NotAcceptable.__init__(self)
            return NotAcceptable( message )

    elif framework and framework.__name__ == "itty":
        if status == 404:
            return framework.NotFound( message )

        if status == 406:
            class NotAcceptable( itty.RequestError ):
                status  = 406
            return NotAcceptable( message )

    return Exception( "%d %s" % ( status, message ))


def html_head( thing, head="<title>%(title)s</title>", **kwargs ):
    """Emit our minimal HTML5 wrapping.  The default 'head' requires only a
    'title' keyword parameter.  <html>, <head> and <body> are all implied."""
    prefix		= """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    """ + ( head  % kwargs ) + """
</head>
<body>
"""

    postfix		= """
</body>
</html>
"""
    return prefix + thing + postfix

def html_wrap( thing, tag="div", **kwargs ):
    """Wrap a thing in a standard HTML <tag>...</tag>, with optional attributes"""
    prefix		= "<"
    prefix     	       += tag
    for attr, value in kwargs.items():
        prefix	       += " %s='%s'" ( attr, value )
    prefix	       += ">\n"
    return prefix + thing + "\n</%s>\n" % tag

#
# URL request handlers
#
#     device_request	-- Returns all specified, after executing (optional) command
# 
# 
#   group / match / command / value	description
#   -----   -----   -------   -----	----------- 
#   cip   / o.i.a / value[x]/ 1000	Set the given object, instance, attribute's value[x] to 1000
#   cip   / <tag> / value   / [1,2,3]
# 
#   option/ delay / value   / 1.2	Set the option delay.value=1.2
# 
# 
def api_request( group, match, command, value,
                      queries=None, environ=None, accept=None,
                      framework=None ):
    """Return a JSON object:
      {
        data:     [ {}, ... ]
        messages: [ "2012-12-21 11:22:33 M46 Motor Fault", ... ]
      }

    The data list contains objects representing all matching objects, executing
    the optional command.  If an accept encoding is supplied, use it.
    Otherwise, detect it from the environ's' "HTTP_ACCEPT"; default to
    "application/json".

        group		-- A device group, w/globbing; no default
        match		-- A device id match, w/globbing; default is: '*'
        command		-- The command to execute on the device; default is: 'get'
        value		-- All remaining query parameters; default is: []

        queries		-- All HTTP queries and/or form parameters
        environ		-- The HTTP request environment
        accept		-- A forced MIME encoding (eg. application/json).
        framework	-- The web framework module being used
    """

    global options
    global connections
    global tags
    accept		= deduce_encoding( [ "application/json",
                                             "text/javascript",
                                             "text/plain",
                                             "text/html" ],
                                           environ=environ, accept=accept )

    # Deduce the device group and id match, and the optional command and value.
    # Values provided on the URL are overridden by those provided as query options.
    if "group" in queries and queries["group"]:
        group		= queries["group"]
        del queries["group"]
    if not group:
        group		= "*"

    if "match" in queries and queries["match"]:
        match		= queries["match"]
        del queries["match"]
    if not match:
        match		= "*" 

    # The command/value defaults to the HTTP request, but also may be overridden by
    # the query option.
    if "command" in queries and queries["command"]:
        command		= queries["command"]
        del queries["command"]
    if "value" in queries and queries["value"]:
        value		= queries["value"]
        del queries["value"]

    # The "since" query option may be supplied, and is used to prevent (None) or
    # limit (0,...)  the "alarm" responses to those that have been updated/added
    # since the specified time.
    since		= None
    if "since" in queries and queries["since"]:
        since		= float( queries["since"] )
        del queries["since"]

    # Collect up all the matching objects, execute any command, and then get
    # their attributes, adding any command { success: ..., message: ... }
    now			= timer()
    content		= {
        "alarm":	[],
        "command":	None,
        "data":		{},
        "since":	since,		# time, 0, None (null)
        "until":	timer(),	# time (default, unless we return alarms)
        }

    logging.debug( "Searching for %s/%s, since: %s (%s)" % (
            group, match, since, 
            None if since is None else time.ctime( since )))

    # Effectively:
    #     group.match.command = value
    # Look through each "group" object's dir of available attributes for "match".  Then, see if 
    # that target attribute exists, and is an instance of dict.
    for grp, obj in [ 
            ('options',		options),
            ('connections', 	connections),
            ('tags',		tags )]: 
      for mch in [ m for m in dir( obj ) if not m.startswith( '_' ) ]:
        log.detail( "Evaluating %s.%s: %r", grp, mch, getattr( obj, mch, None ))
        if not fnmatch.fnmatch( grp, group ):
            continue
        if not fnmatch.fnmatch( mch, match ):
            continue
        target		= getattr( obj, mch, None )
        if not target:
            log.warning( "Couldn't find advertised attribute %s.%s", grp, mch )
            continue
        if not isinstance( target, dict ):
            continue

        # The dct's group name 'nam' matches requested group (glob), and the dct 'key' matches
        # request match (glob).   /<group>/<match> matches this dct[key].
        result		= {}
        if command and command.lower() != "get":
            try:
                # Retain the same type as the current value, and allow indexing!  We want to ensure that we don't cause
                # failures by corrupting the types of value.  Unfortunately, this makes it tricky to
                # support "bool", as coercion from string is not supported.
                cur	= getattr( target, command )
                log.normal( "%s/%s: Setting %s to %r (was %r)" % ( grp, mch, command, value, cur ))
                typ	= type( cur )
                try:
                    cvt	= typ( value )
                except TypeError:
                    if typ is bool:
                        # Either 'true'/'yes' or 'false'/'no', is acceptable, otherwise it better be
                        # a number
                        if value.lower() in ('true', 'yes'):
                            cvt = True
                        elif value.lower() in ('false', 'no'):
                            cvt = False
                        else:
                            cvt = bool( int( value ))
                    else:
                        raise
                setattr( target, command, cvt )
                result["success"] = True
                result["message"] = "%s.%s.%s=%r (%r) successful" % ( grp, mch, command, value, cvt )
            except Exception as exc:
                result["success"] = False
                result["message"] = "%s.%s..%s=%r failed: %s" % ( grp, mch, command, value, exc )
                logging.warning( "%s.%s..%s=%s failed: %s\n%s" % ( grp, mch, command, value, exc,
                                                                   traceback.format_exc() ))

        # Get all of target's attributes (except _*) advertised by its dir() results
        attrs		= [ a for a in dir( target ) if not a.startswith('_') ]
        data		= { a: getattr( target, a ) for a in attrs }
        content["command"] = result
        content["data"].setdefault( grp, {} )[mch] = data

        # and capture each of the target._events() in content["alarms"], purging
        # old events, and producing only those events since the supplied time.
        if hasattr( target, '_events' ):
            content["alarm"].extend( target._events( since=since, purge=True ))
    content["alarm"].sort( key=lambda event: event["time"], reverse=True )

    # Report the end of the time-span of alarm results returned; if none, then
    # the default time will be the _timer() at beginning of this function.  This
    # ensures we don't duplicately report alarms (or miss any)
    if content["alarm"]:
        content["until"]= content["alarm"][0]["time"]

    # JSON
    response            = json.dumps( content, sort_keys=True, indent=4, default=lambda obj: repr( obj ))

    if accept in ("text/html"):
        # HTML; dump any request query options, wrap JSON response in <pre>
        response	= html_wrap( "Response:", "h2" ) \
            		+ html_wrap( response, "pre" )
        response        = html_wrap( "Queries:",  "h2" ) \
            		+ html_wrap( "\n".join(
                            ( "%(query)-16.16s %(value)r" % {
                                "query":	str( query ) + ":",
                                "value":	value,
                                }
                              for iterable in ( queries,
                                                [("group", group),
                                                 ("match", match),
                                                 ("command", command),
                                                 ("value", value),
                                                 ("since", since),
                                                 ] )
                                  for query, value in iterable )), tag="pre" ) \
                  	+ response
        response        = html_head( response,
                                     title='/'.join( ["api", group, match, command] ))
    elif accept and accept not in ("application/json", "text/javascript", "text/plain"):
        # Invalid encoding requested.  Return appropriate 406 Not Acceptable
        message		=  "Invalid encoding: %s, for Accept: %s" % (
            accept, environ.get( "HTTP_ACCEPT", "*.*" ))
        raise http_exception( framework, 406, message )

    # Return the content-type we've agreed to produce, and the result.
    return accept, response


# 
# The web.py url endpoints, and their classes
# 
class trailing_slash:
    def GET( self, path ):
        web.seeother( path )

class favicon:
    def GET( self ):
        """Always permanently redirect favicon.ico requests to our favicon.png.
        The reason we do this instead of putting a <link "icon"...> is because
        all *other* requests from browsers (ie. api/... ) returning non-HTML
        response Content-Types such as application/json *also* request
        favicon.ico, and we don't have an HTML <head> to specify any icon link.
        Furthermore, they continue to request it 'til satisfied, so we do a 301
        Permanent Redirect to satisfy the browser and prevent future requests.
        So, this is the most general way to handle the favicon.ico"""
        web.redirect( '/static/images/favicon.png' )

class home:
    def GET( self ):
        """Forward to an appropriate start page.  Detect if behind a
        proxy, and use the original forwarded host.
        """
        # print json.dumps(web.ctx, skipkeys=True, default=repr, indent=4,)
        proxy		= web.ctx.environ.get( "HTTP_X_FORWARDED_HOST", "" )
        if proxy:
            proxy	= "http://" + proxy
        target		= proxy + "/static/index.html"
        web.seeother( target )

class api:
    def GET( self, *args ):
        """Expects exactly 4 arguments, all of which may be empty, or
        contain a / followed by 0 or more non-/ characters.  Deduce
        accept encoding from Accept: header, or force JSON if .json path
        was explicitly requested.  These 4 arguments are the device
        group and id patterns, followed by the optional command and
        value.

        Always returns a content-type and response; virtually all
        failures involving problems with the device, PLC or
        communications are expected to return a successful 200 response,
        with a JSON payload describing the command success state, and a
        message describing any failure mode.  This includes
        communication failures (eg. LAN disruptions, PLC failures,
        etc.), incorrect commands to devices (eg. writing to a read-only
        attribute, etc.)

        If an exception is raised (due to some other internal failure),
        it should be an appropriate one from the supplied framework to
        carry a meaningful HTTP status code.  Otherwise, a generic 500
        Server Error will be produced.  We expect that non-200 response
        failures are due to some unexpected failure, and should
        eventually lead to a system restart.
        """
        environ		= web.ctx.environ
        queries		= web.input()

        # Ensure these are set, regardless of result
        web.header( "Cache-Control", "no-cache" )
        web.header( "Access-Control-Allow-Origin", "*" )

        # The last parameter may end with '.json', and forces accept to
        # "application/json".  Ensure every empty parameter is None.  If
        # exactly 4 args are not supplied, we'll produce a 500 Server
        # Error.  'command' defaults to the HTTP request if not set.
        def clean( a ):
            if a:
                if a.startswith( "/" ):
                    a		= a[1:]
                if a.endswith( ".json" ):
                    a		= a[:-5]
                    clean.accept= "application/json"
            else:
                a		= None
            return a
        clean.accept		= None

        try:
            group, match, command, value \
			= [ clean( a ) for a in args ]
        except:
            raise http_exception( 500, "/api requires 4 arguments" )
        if not command:
            command		= 'get'

        log.detail( "group: %s, match: %s, command: %s, value: %s, accept: %s",
                    group, match, command, value, clean.accept )
            
        content, response = api_request( group=group, match=match,
                                            command=command, value=value,
                                            queries=queries, environ=environ,
                                            accept=clean.accept, framework=web )
        web.header( "Content-Type", content )
        return response


urls				= (
    "(/.*)/",					"trailing_slash",
    "/favicon.ico",				"favicon",
    "/api(/[^/]*)?(/[^/]*)?(/[^/]*)?(/.*)?",	"api",
    "/?",					"home",
)


def web_api( http=None):
    """Get the required web.py classes from the global namespace.  The iface:port must always passed on
    argv[1] to use app.run(), so use lower-level web.httpserver.runsimple interface, so we can bind
    to the supplied http address."""
    try:
        app			= web.application( urls, globals() )
        web.httpserver.runsimple( app.wsgifunc(), http )
        log.normal( "Web API started on %s:%s", http[0], http[1] )
    except socket.error:
        log.error( "Could not bind to %s:%s for web API", http[0], http[1] )
    except Exception as exc:
        log.error( "Web API server on %s:%s failed: %s", http[0], http[1], exc )


# 
# The EtherNet/IP CIP Main and Server Thread
# 
#     An instance of this function runs in a Thread for each active connection.
# 
def enip_srv( conn, addr, enip_process=None, delay=None, **kwds ):
    """Serve one Ethernet/IP client 'til EOF; then close the socket.  Parses headers and encapsulated
    EtherNet/IP request data 'til either the parser fails (the Client has submitted an un-parsable
    request), or the request handler fails.  Otherwise, encodes the data.response in an EtherNet/IP
    packet and sends it back to the client.

    Use the supplied enip_process function to process each parsed EtherNet/IP frame, returning True
    if a data.response is formulated, False if the session has ended cleanly, or raise an Exception
    if there is a processing failure (eg. an unparsable request, indicating that the Client is
    speaking an unknown dialect and the session must close catastrophically.)

    If a partial EtherNet/IP header is parsed and an EOF is received, the enip_header parser will
    raise an AssertionError, and we'll simply drop the connection.  If we receive a valid header and
    request, the supplied enip_process function is expected to formulate an appropriate error
    response, and we'll continue processing requests.

    An option numeric delay value (or any delay object with a .value attribute evaluating to a
    numeric value) may be specified; every response will be delayed by the specified number of
    seconds.  We assume that such a value may be altered over time, so we access it afresh for each
    use.

    All remaining keywords are passed along to the supplied enip_process function.
    """
    name			= "enip_%s" % addr[1]
    log.normal( "EtherNet/IP Server %s begins serving peer %s", name, addr )


    source			= cpppo.rememberable()
    with parser.enip_machine( name=name, context='enip' ) as enip_mesg:

        # We can be provided a dotdict() to contain our stats.  If one has been passed in, then this
        # means that our stats for this connection will be available to the web API; it may set
        # stats.eof to True at any time, terminating the connection!  The web API will try to coerce its
        # input into the same type as the variable, so we'll keep it an int (type bool doesn't handle
        # coercion from strings)
        stats			= cpppo.dotdict()
        connkey			= ( "%s_%d" % addr ).replace( '.', '_' )
        connections[connkey]	= stats
        try:
            assert enip_process is not None, \
                "Must specify an EtherNet/IP processing function via 'enip_process'"
            stats.requests	= 0
            stats.received	= 0
            stats.eof		= False
            stats.interface	= addr[0]
            stats.port		= addr[1]
            while not stats.eof:
                data		= cpppo.dotdict()

                source.forget()
                # If no/partial EtherNet/IP header received, parsing will fail with a NonTerminal
                # Exception (dfa exits in non-terminal state).  Build data.request.enip:
                for mch,sta in enip_mesg.run( path='request', source=source, data=data ):
                    if sta is None:
                        # No more transitions available.  Wait for input.  EOF (b'') will lead to
                        # termination.  We will simulate non-blocking by looping on None (so we can
                        # check our options, in case they've been changed).  If we still have input
                        # available to process right now in 'source', we'll just check (0 timeout).
                        msg	= None
                        while msg is None and not stats.eof:
                            msg	= network.recv( conn, timeout=.1 if source.peek() is None else 0 )
                            if msg is not None:
                                stats.received += len( msg )
                                stats.eof       = stats.eof or not len( msg )
                                log.detail( "%s recv: %5d: %s", enip_mesg.name_centered(),
                                            len( msg ) if msg is not None else 0, reprlib.repr( msg ))
                                source.chain( msg )
                            else:
                                # No input.  If we have input available, no problem; continue.  This
                                # can occur if the state machine cannot make a transition on the
                                # input symbol, indicating an unacceptable sentence for the grammar.
                                # If it cannot make progress, the machine will terminate in a
                                # non-terminal state, rejecting the sentence.
                                if source.peek() is not None:
                                    break
                                # We're at a None (can't proceed), and no input is available.  This
                                # is where we implement "Blocking"; just loop.

                # Terminal state and EtherNet/IP header recognized, or clean EOF (no partial
                # message); process and return response
                log.info( "%s req. data: %s", enip_mesg.name_centered(), parser.enip_format( data ))
                if 'request' in data:
                    stats.requests += 1
                try:
                    # enip_process must be able to handle no request (empty data), indicating the
                    # clean termination of the session if closed from this end (not required if
                    # enip_process returned False, indicating the connection was terminated by request.)
                    if enip_process( addr, data=data, **kwds ):
                        # Produce an EtherNet/IP response carrying the encapsulated response data.
                        assert 'response' in data, "Expected EtherNet/IP response; none found"
                        assert 'enip.input' in data.response, "Expected EtherNet/IP response encapsulated message; none found"
                        rpy	= parser.enip_encode( data.response.enip )
                        log.detail( "%s send: %5d: %s %s", enip_mesg.name_centered(),
                                    len( rpy ), reprlib.repr( rpy ),
                                    ("delay: %r" % delay) if delay else "" )
                        if delay:
                            # A delay (anything with a delay.value attribute) == #[.#] (converible
                            # to float) is ok; may be changed via web interface.
                            try:
                                seconds = float( delay.value if hasattr( delay, 'value' ) else delay )
                                time.sleep( seconds )
                            except Exception as exc:
                                log.detail( "Unable to delay; invalid seconds: %r", delay )
                        try:
                            conn.send( rpy )
                        except socket.error as exc:
                            log.detail( "%s session ended (client abandoned): %s", enip_mesg.name_centered(), exc )
                            eof	= True
                    else:
                        # Session terminated.  No response, just drop connection.
                        log.detail( "%s session ended (client initiated): %s", enip_mesg.name_centered(), parser.enip_format( data ))
                        eof	= True
                except:
                    log.error( "Failed request: %s", parser.enip_format( data ))
                    enip_process( addr, data=cpppo.dotdict() ) # Terminate.
                    raise

            stats.processed	= source.sent
        except:
            # Parsing failure.  We're done.  Suck out some remaining input to give us some context.
            stats.processed	= source.sent
            memory		= bytes(bytearray(source.memory))
            pos			= len( source.memory )
            future		= bytes(bytearray( b for b in source ))
            where		= "at %d total bytes:\n%s\n%s (byte %d)" % (
                stats.processed, repr(memory+future), '-' * (len(repr(memory))-1) + '^', pos )
            log.error( "EtherNet/IP error %s\n\nFailed with exception:\n%s\n", where,
                         ''.join( traceback.format_exception( *sys.exc_info() )))
            raise
        finally:
            # Not strictly necessary to close (network.server_main will discard the socket,
            # implicitly closing it), but we'll do it explicitly here in case the thread doesn't die
            # for some other reason.  Clean up the connections entry for this connection address.
            connections.pop( connkey, None )
            log.normal( "%s done; processed %3d request%s over %5d byte%s/%5d received (%d connections remain)", name,
                        stats.requests,  " " if stats.requests == 1  else "s",
                        stats.processed, " " if stats.processed == 1 else "s", stats.received,
                        len( connections ))
            sys.stdout.flush()
            conn.close()


# 
# main		-- Run the EtherNet/IP Controller Simulation
# 
#     Pass the desired argv (excluding the program name in sys.arg[0]; typically pass
# argv=sys.argv[1:]); requires at least one tag to be defined.
# 
def main( argv=None, **kwds ):

    global address
    global options
    global tags

    if argv is None:
        argv			= []

    ap				= argparse.ArgumentParser(
        description = "Provide an EtherNet/IP Server",
        epilog = "" )

    ap.add_argument( '-v', '--verbose',
                     default=0, action="count",
                     help="Display logging information." )
    ap.add_argument( '-a', '--address',
                     default=( "%s:%d" % address ),
                     help="EtherNet/IP interface[:port] to bind to (default: %s:%d)" % (
                         address[0], address[1] ))
    ap.add_argument( '-l', '--log',
                     help="Log file, if desired" )
    ap.add_argument( '-w', '--web',
                     default="",
                     help="Web API [interface]:[port] to bind to (default: %s, port 80)" % (
                         address[0] ))
    ap.add_argument( '-d', '--delay',
                     help="Delay response to each request by a certain number of seconds (default: 0.0)",
                     default="0.0" )
    ap.add_argument( 'tags', nargs="+",
                     help="Any tags, their type (default: INT), and number (default: 1), eg: tag=INT[1000]")

    args			= ap.parse_args( argv )


    # Deduce interface:port address to bind, and correct types (default is address, above)
    bind			= args.address.split(':')
    assert 1 <= len( bind ) <= 2, "Invalid --address [<interface>]:[<port>}: %s" % args.address
    bind			= ( str( bind[0] ) if bind[0] else address[0],
                                    int( bind[1] ) if len( bind ) > 1 and bind[1] else address[1] )

    # Set up logging level (-v...) and --log <file>
    levelmap 			= {
        0: logging.WARNING,
        1: logging.NORMAL,
        2: logging.DETAIL,
        3: logging.INFO,
        4: logging.DEBUG,
        }
    level			= ( levelmap[args.verbose] 
                                    if args.verbose in levelmap
                                    else logging.DEBUG )
    rootlog			= logging.getLogger("")
    rootlog.setLevel( level )
    if args.log:
        formatter		= rootlog.handlers[0].formatter
        while len( rootlog.handlers ):
            rootlog.removeHandler( rootlog.handlers[0] )
        handler			= logging.handlers.RotatingFileHandler( args.log, maxBytes=10*1024*1024, backupCount=5 )
        handler.setFormatter( formatter )
        rootlog.addHandler( handler )


    # Global options data.  First, copy any keyword args supplied to main().  This could include an
    # alternative enip_process, for example, instead of defaulting to logix.process.
    options.update( kwds )

    # Specify a response delay.  The options.delay is another dotdict() layer, so it's attributes
    # (eg. .value, .range) are available to the web API for manipulation.  Therefore, they can be
    # set to arbitrary values at random times!  However, the type will be retained.
    def delay_range( *args, **kwds ):
        """If a delay.range like ".1-.9" is specified, then change the delay.value every second to something
        in that range."""
        assert 'delay' in kwds and 'range' in kwds['delay'] and '-' in kwds['delay']['range'], \
            "No delay=#-# specified"
        log.normal( "Delaying all responses by %s seconds", kwds['delay']['range'] )
        while True:
            # Once we start, changes to delay.range will be re-evaluated each loop
            time.sleep( 1 )
            try:
                lo,hi		= map( float, kwds['delay']['range'].split( '-' ))
                kwds['delay']['value'] = random.uniform( lo, hi )
                log.info( "Mutated delay == %g", kwds['delay']['value'] )
            except Exception as exc:
                log.warning( "No delay=#[.#]-#[.#] range specified: %s", exc )

    options.delay		= cpppo.dotdict()
    try:
        options.delay.value	= float( args.delay )
        log.normal( "Delaying all responses by %r seconds" , options.delay.value )
    except:
        assert '-' in args.delay, \
            "Unrecognized --delay=%r option" % args.delay
        # A range #-#; set up a thread to mutate the option.delay.value over the .range
        options.delay.range	= args.delay
        options.delay.value	= 0.0
        mutator			= threading.Thread( target=delay_range, kwargs=options )
        mutator.daemon		= True
        mutator.start()

    # Create all the specified tags/Attributes.  The enip_process function will (somehow) assign the
    # given tag name to reference the specified Attribute.
    for t in args.tags:
        tag_name, rest		= t, ''
        if '=' in tag_name:
            tag_name, rest	= tag_name.split( '=', 1 )
        tag_type, rest		= rest or 'INT', ''
        tag_size		= 1
        if '[' in tag_type:
            tag_type, rest	= tag_type.split( '[', 1 )
            assert ']' in rest, "Invalid tag; mis-matched [...]"
            tag_size, rest	= rest.split( ']', 1 )
        assert not rest, "Invalid tag specified; expected tag=<type>[<size>]: %r" % t
        tag_type		= str( tag_type ).upper()
        typenames		= {"INT": parser.INT, "DINT": parser.DINT, "SINT": parser.SINT }
        assert tag_type in typenames, "Invalid tag type; must be one of %r" % list( typenames.keys() )
        try:
            tag_size		= int( tag_size )
        except:
            raise AssertionError( "Invalid tag size: %r" % tag_size )

        # Ready to create the tag and its Attribute (and error code to return, if any)
        log.normal( "Creating tag: %s=%s[%d]", tag_name, tag_type, tag_size )
        tags[tag_name]		= cpppo.dotdict()
        tags[tag_name].attribute= device.Attribute( tag_name, typenames[tag_type],
                                                    default=( 0 if tag_size == 1 
                                                              else [0 for i in range( tag_size )]))
        tags[tag_name].error	= 0x00
    log.normal( "EtherNet/IP Tags defined: %r", tags )

    # Use the Logix simulator by default (unless some other one was supplied as a keyword options to
    # main(), loaded above into 'options').  This key indexes an immutable value (not another dotdict
    # layer), so is not available for the web API to report/manipulate.
    options.setdefault( 'enip_process', logix.process )

    # The Web API

    # Deduce web interface:port address to bind, and correct types (default is address, above).
    # Default to the same interface as we're bound to, port 80.  We'll only start if non-empty --web
    # was provided, though (even if it's just ':', to get all defaults).  Usually you'll want to
    # specify at least --web :[<port>].
    http			= args.web.split(':')
    assert 1 <= len( http ) <= 2, "Invalid --web [<interface>]:[<port>}: %s" % args.web
    http			= ( str( http[0] ) if http[0] else bind[0],
                                    int( http[1] ) if len( http ) > 1 and http[1] else 80 )

    logging.info( "EtherNet/IP Simulator Web API Server: %r" % ( address, ))

    if args.web:
        webserver		= threading.Thread( target=web_api, kwargs={'http': http} )
        webserver.daemon	= True
        webserver.start()

        
    # The EtherNet/IP Simulator.  Pass all the top-level options keys/values as keywords, and pass
    # the entire tags dotdict as a tags=... keyword.
    kwargs			= dict( options, tags=tags )
    log.detail( "Keywords to EtherNet/IP Server: %r", kwargs )
    return network.server_main( address=bind, target=enip_srv, kwargs=kwargs )


if __name__ == "__main__":
    sys.exit( main() )