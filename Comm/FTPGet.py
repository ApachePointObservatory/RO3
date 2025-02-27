
"""Retrieve a remote file via ftp to a local file.

The retrieval occurs in a background thread.

Note: I originally had abort in its own thread, but this sometimes failed
in a nasty way. It turns out to be unsafe to close a file in one thread
while it is being read in another thread.

Note: originally use urllib, but a nasty bug in urllib (Python 2.3 and 2.4b1)
prevented multiple transfers from working reliably.

To Do:
- add atexit handler that kills any ongoing transfers
  (wants a new function that keeps track of FTPGet objects
  that are currently downloading)

History:
2003-09-25 ROwen
2003-10-06 ROwen    Changed background threads to daemonic, for fast exit
2003-10-16 ROwen    Bug fix: createDir mis-referenced (thanks, Craig Loomis).
2004-05-18 ROwen    Bug fix: used sys for reporting errors but did not import it.
2004-11-17 ROwen    Renamed from FTPGet and overhauled to use ftplib
                    and consequently an entirely different interface.
2004-11-19 ROwen    Bug fix: was not setting TYPE I for binary.
2004-12-14 ROwen    Minor change to a debug string.
2005-05-23 ROwen    Modified to not check for "file exists" until download starts.
                    The old behavior made error checking too messy.
2005-06-13 ROwen    Removed support for callbacks. These were called
                    from a background thread, and so were not Tk-safe.
2005-07-07 ROwen    Bug fix: if overwrite false, the transfer would fail
                    but the existing file would still be deleted.
2012-08-01 ROwen    Changed getStateStr() -> state, isDone()->isDone, getReadBytes()->readBytes, getTotBytes()->totBytes.
                    Removed getState.
                    Added isAbortable.
                    State constants are now FTPGet class variables instead of module globals.
                    State constants are now strings instead of integers.
2014-09-17 ROwen    Bug fix: most state constants missing self. prefix.
2015-09-24 ROwen    Replace "== None" with "is None" to modernize the code.
2015-11-03 ROwen    Replace "!= None" with "is not None" to modernize the code.
"""
__all__ = ['FTPGet']

import os
import sys
import urllib.parse
import threading
import ftplib

_Debug = False

class FTPGet:
    """Retrieves the specified url to a file.
    
    Inputs:
    - host  IP address of ftp host
    - fromPath  full path of file on host to retrieve
    - toPath    full path of destination file
    - isBinary  file is binary? (if False, EOL translation is probably performed)
    - overwrite: if True, overwrites the destination file if it exists;
        otherwise raises ValueError
    - createDir: if True, creates any required directories;
        otherwise raises ValueError
    - startNow: if True, the transfer is started immediately
        otherwise callFunc is called and the transaction remains Queued
        until start is called
    - dispStr   a string to display while downloading the file;
                if omitted, an ftp URL (with no username/password) is created
    - username  the usual; *NOT SECURE*
    - password  the usual; *NOT SECURE*
    """

    # state constants
    Queued = "Queued"
    Connecting = "Connecting"
    Running = "Running"
    Aborting = "Aborting"
    Done = "Done"
    Aborted = "Aborted"
    Failed = "Failed"
    
    _AllStates = set((
        Queued,
        Connecting,
        Running,
        Aborting,
        Done,
        Aborted,
        Failed,
    ))
    _AbortableStates = set((Queued, Connecting, Running))
    _DoneStates = set((Done, Aborted, Failed))

    StateStrMaxLen = 0
    for _stateStr in _AllStates:
        StateStrMaxLen = max(StateStrMaxLen, len(_stateStr))
    del(_stateStr)
    def __init__(self,
        host,
        fromPath,
        toPath,
        isBinary = True,
        overwrite = False,
        createDir = True,
        startNow = True,
        dispStr = None,
        username = None,
        password = None,
    ):
        self.host = host
        self.fromPath = fromPath
        self.toPath = toPath
        self.isBinary = isBinary
        self.overwrite = bool(overwrite)
        self.createDir = createDir
        self.username = username or "anonymous"
        self.password = password or "abc@def.org"
        
        if dispStr is None:
            self.dispStr = urllib.parse.urljoin("ftp://" + self.host, self.fromPath)
        else:
            self.dispStr = dispStr

        self._fromSocket = None
        self._toFile = None
        self._readBytes = 0
        self._totBytes = None
        self._state = self.Queued
        self._exception = None
        self._stateLock = threading.RLock()
        
        # set up background thread
        self._getThread = threading.Thread(name="get", target=self._getTask)
        self._getThread.setDaemon(True)

        if startNow:
            self.start()
                    
    def start(self):
        """Start the download.
        
        If state is not Queued, raises RuntimeError
        """
        self._stateLock.acquire()
        try:
            if self._state != self.Queued:
                raise RuntimeError("state = %r not Queued" % (self._state,))
            self._state = self.Connecting
        finally:
            self._stateLock.release()
        self._getThread.start()

    def abort(self):
        """Start aborting: cancel the transaction and delete the output file.
        Silently fails if the transaction has already completed
        """
        self._stateLock.acquire()
        try:
            if self._state == self.Queued:
                self._state = self.Aborted
            elif self._state > 0:
                self._state = self.Aborting
            else:
                return
        finally:
            self._stateLock.release()   

    def getException(self):
        """If the state is Failed, returns the exception that caused the failure.
        Otherwise returns None.
        """
        return self._exception
    
    @property
    def isAbortable(self):
        """True if the transaction can be aborted
        """
        return self._state in self._AbortableStates
    
    @property
    def isDone(self):
        """True if the transaction is finished (succeeded, aborted or failed), False otherwise.
        """
        return self._state in self._DoneStates
    
    @property
    def readBytes(self):
        """bytes read so far
        """
        return self._readBytes

    @property
    def totBytes(self):
        """total bytes in file, if known, None otherwise.
        
        The value is certain to be unknown until the transfer starts;
        after that it depends on whether the server sends the info.
        """
        return self._totBytes
    
    @property
    def state(self):
        """Current state, as a string
        """
        return self._state
    
    def _cleanup(self, newState, exception=None):
        """Clean up everything. Must only be called from the _getTask thread.
        
        Close the input and output files.
        If not isDone (transfer not finished) then updates the state
        If newState in (Aborted, Failed) and not isDone, deletes the file
        If newState == Failed and not isDone, sets the exception
        
        Inputs:
        - newState: new state; ignored if isDone
        - exception: exception that is the reason for failure;
            ignored unless newState = Failed and not isDone
        """
        if _Debug:
            print("_cleanup(%r, %r)" % (newState, exception))
        didOpen = (self._toFile is not None)
        if self._toFile:
            self._toFile.close()
            self._toFile = None
        if _Debug:
            print("_toFile closed")
        if self._fromSocket:
            self._fromSocket.close()
            self._fromSocket = None
        if _Debug:
            print("_fromSocket closed")

        # if state is not valid, warn and set to Failed
        if newState not in self._DoneStates:
            sys.stderr.write("FTPGet._cleanup invalid cleanup state %r; assuming %s=Failed\n" % \
                (newState, self.Failed))
            newState = self.Failed
        
        self._stateLock.acquire()   
        try:
            if self.isDone:
                # already finished; do nothing
                return
            else:
                self._state = newState
        finally:
            self._stateLock.release()
        
        if didOpen and newState in (self.Aborted, self.Failed):
            try:
                os.remove(self.toPath)
            except OSError:
                pass
            
            if newState == self.Failed:
                self._exception = exception

    def _getTask(self):
        """Retrieve the file in a background thread.
        Do not call directly; use start() instead.
        """
        try:
            if _Debug:
                print("FTPGet: _getTask begins")

            # verify output file and verify/create output directory, as appropriate
            self._toPrep()

            # open output file
            if _Debug:
                print("FTPGet: opening output file %r" % (self.toPath,))
            if self.isBinary:
                mode = "wb"
            else:
                mode = "w"
            self._toFile = open(self.toPath, mode)

            # open input socket
            if _Debug:
                print("FTPGet: open ftp connection to %r" % (self.host))
            ftp = ftplib.FTP(self.host, self.username, self.password)
            
            if _Debug:
                print("FTPGet: set connection isbinary=%r on %r" % (self.isBinary, self.host))
            if self.isBinary:
                ftp.voidcmd("TYPE I")
            else:
                ftp.voidcmd("TYPE A")

            if _Debug:
                print("FTPGet: open socket to %r on %r" % (self.fromPath, self.host))
            self._fromSocket, self._totBytes = ftp.ntransfercmd('RETR %s' % self.fromPath)

            self._stateLock.acquire()
            try:
                self._state = self.Running
            finally:
                self._stateLock.release()   

            if _Debug:
                print("FTPGet: totBytes = %r; read %r on %r " % \
                    (self._totBytes, self.fromPath, self.host))
            
            while True:
                nextData = self._fromSocket.recv(8192)
                if not nextData:
                    break
                elif self._state == self.Aborting:
                    self._cleanup(self.Aborted)
                    return
                self._readBytes += len(nextData)
                self._toFile.write(nextData)
            
            self._cleanup(self.Done)
        except Exception as e:
            self._cleanup(self.Failed, exception = e)
        
    
    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.fromPath)

    def _toPrep(self):
        """Create or verify the existence of the output directory
        and check if output file already exists.
        
        Raises an exception if anything is wrong.
        """
        # if output file exists and not overwrite, complain
        if not self.overwrite and os.path.exists(self.toPath):
            raise ValueError("toPath %r already exists" % (self.toPath,))
        
        # if directory does not exist, create it or fail, depending on createDir;
        # else if "directory" exists but is a file, fail
        toDir = os.path.dirname(self.toPath)
        if toDir:
            if not os.path.exists(toDir):
                # create the directory or fail, depending on createDir
                if self.createDir:
                    os.makedirs(toDir)
                else:
                    raise ValueError("directory %r does not exist" % (toDir,))
            elif not os.path.isdir(toDir):
                raise RuntimeError("%r is a file, not a directory" % (toDir,))
