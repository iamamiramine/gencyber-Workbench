/* Interactive shell with persistent sessions */

/*
* This program creates persistent terminal sessions that can be shared
* across multiple socket connections and survive client disconnections.
*/

var express = require('express')
const pty = require('node-pty');
var app = express();
var fs = require('fs');
var bodyParser = require('body-parser');
var http = require('http').createServer(app);
var io = require('socket.io')(http, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"],
    allowedHeaders: ["*"],
    credentials: true
  },
  transports: ['polling', 'websocket'],
  allowEIO3: true,
  pingTimeout: 30000,
  pingInterval: 10000,
  cookie: false,
  serveClient: true,
  path: '/socket.io/',
  upgradeTimeout: 30000,
  maxHttpBufferSize: 1e6
});

app.use(bodyParser.json());
app.use(bodyParser.urlencoded({
  extended: true
}));
app.use(express.static(__dirname + '/public'));

// Enable CORS for all routes
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization');
  if (req.method === 'OPTIONS') {
    res.sendStatus(200);
  } else {
    next();
  }
});

var startDir = false;
if (process.argv.length >= 3) {
  startDir = process.argv[2];
}

// Global session storage
const terminalSessions = new Map();
const sessionClients = new Map(); // Track clients per session

// Create default session on startup
function createDefaultSession() {
  const defaultSessionId = 'default';
  if (!terminalSessions.has(defaultSessionId)) {
    getOrCreateSession(defaultSessionId);
  }
}

// Clean up old sessions periodically
setInterval(() => {
  cleanupOldSessions();
}, 60000); // Every minute

function cleanupOldSessions() {
  const now = Date.now();
  const maxAge = 30 * 60 * 1000; // 30 minutes

  for (const [sessionId, session] of terminalSessions.entries()) {
    const clientCount = sessionClients.get(sessionId)?.size || 0;
    const lastActivity = session.lastActivity || session.createdAt;
    
    // Don't cleanup the default session or sessions with active clients
    if (sessionId === 'default') continue;
    
    // Only cleanup sessions with no clients and old activity
    if (clientCount === 0 && (now - lastActivity) > maxAge) {
      destroySession(sessionId);
    }
  }
}

function destroySession(sessionId) {
  const session = terminalSessions.get(sessionId);
  if (session && session.terminal) {
    try {
      session.terminal.kill('SIGTERM');
    } catch (e) {
      // Ignore errors when killing terminal
    }
  }
  terminalSessions.delete(sessionId);
  sessionClients.delete(sessionId);
}

// Create or get existing terminal session
function getOrCreateSession(sessionId, cwd = null) {
  if (terminalSessions.has(sessionId)) {
    const session = terminalSessions.get(sessionId);
    session.lastActivity = Date.now();
    return session;
  }
  
  // Determine working directory
  const workingDir = cwd || startDir || process.cwd();
  
  // Create new pseudo-terminal process
  const terminal = pty.spawn('/bin/bash', [], {
    name: 'xterm-256color',
    cols: 120,
    rows: 30,
    cwd: workingDir,
    handleFlowControl: true,
    env: { 
      ...process.env, 
      TERM: 'xterm-256color',
      LANG: 'en_US.UTF-8',
      SHELL: '/bin/bash',
      USER: process.env.USER || 'root',
      HOME: process.env.HOME || '/root',
      LC_ALL: 'en_US.UTF-8',
      PS1: '[\\u@\\h \\W]\\$ ',
      DEBIAN_FRONTEND: 'noninteractive'
    }
  });

  const session = {
    id: sessionId,
    terminal: terminal,
    createdAt: Date.now(),
    lastActivity: Date.now(),
    outputHistory: [],
    workingDir: workingDir
  };

  // Set up terminal event handlers
  terminal.on('data', (data) => {
    session.outputHistory.push({
      type: 'stdout',
      data: data,
      timestamp: Date.now()
    });
    session.lastActivity = Date.now();
    
    // Broadcast to all clients connected to this session
    broadcastToSession(sessionId, 'terminal-output-stdout', { message: data });
  });

  terminal.on('exit', (code, signal) => {
    session.outputHistory.push({
      type: 'exit',
      data: `Process exited with code ${code}`,
      timestamp: Date.now()
    });
    
    // Broadcast exit to all clients
    broadcastToSession(sessionId, 'terminal-exit-code', { message: code || 1337 });
    
    // Restart the session if it's the default session or has active clients
    if (sessionId === 'default' || (sessionClients.get(sessionId)?.size || 0) > 0) {
      setTimeout(() => {
        const newSession = getOrCreateSession(sessionId, workingDir);
        broadcastToSession(sessionId, 'terminal-output-stdout', { 
          message: '\n--- Terminal session restarted ---\n' 
        });
      }, 1000);
    } else {
      // Clean up the session
      session.terminal = null;
    }
  });

  terminal.on('error', (error) => {
    broadcastToSession(sessionId, 'terminal-output-stderr', { 
      message: `Terminal error: ${error.message}\n` 
    });
  });

  // Keep limited history (last 1000 entries)
  setInterval(() => {
    if (session.outputHistory.length > 1000) {
      session.outputHistory = session.outputHistory.slice(-500);
    }
  }, 30000);

  // Send initial prompt
  setTimeout(() => {
    if (terminal && !terminal.killed) {
      terminal.write('\n');
    }
  }, 500);

  terminalSessions.set(sessionId, session);
  return session;
}

function broadcastToSession(sessionId, event, data) {
  const clients = sessionClients.get(sessionId);
  if (clients) {
    clients.forEach(socket => {
      try {
        if (socket.connected) {
          socket.emit(event, data);
        }
      } catch (error) {
        // Remove dead clients
        clients.delete(socket);
      }
    });
  }
}

function addClientToSession(sessionId, socket) {
  if (!sessionClients.has(sessionId)) {
    sessionClients.set(sessionId, new Set());
  }
  sessionClients.get(sessionId).add(socket);
}

function removeClientFromSession(sessionId, socket) {
  const clients = sessionClients.get(sessionId);
  if (clients) {
    clients.delete(socket);
  }
}

/* Output index file */
app.get('/', function(req, res) {
  res.sendFile(__dirname + '/index.html');
});

app.get('/node_modules/*', function(req, res) {
  res.sendFile(__dirname + req.originalUrl);
});

// Health check endpoint
app.get('/health', function(req, res) {
  res.json({ 
    status: 'ok', 
    timestamp: new Date().toISOString(),
    sessions: terminalSessions.size,
    uptime: process.uptime()
  });
});

// API endpoint to get session info
app.get('/api/sessions', function(req, res) {
  const sessions = [];
  for (const [sessionId, session] of terminalSessions.entries()) {
    const clientCount = sessionClients.get(sessionId)?.size || 0;
    sessions.push({
      id: sessionId,
      createdAt: session.createdAt,
      lastActivity: session.lastActivity,
      clientCount: clientCount,
      isActive: !!session.terminal && !session.terminal.killed
    });
  }
  res.json(sessions);
});

// API endpoint to create a new session
app.post('/api/sessions', function(req, res) {
  const sessionId = req.body.sessionId || `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  const cwd = req.body.cwd;
  
  try {
    const session = getOrCreateSession(sessionId, cwd);
    res.json({
      success: true,
      sessionId: sessionId,
      createdAt: session.createdAt
    });
  } catch (error) {
    res.status(500).json({
      success: false,
      error: error.message
    });
  }
});

// API endpoint to execute a command via HTTP
app.post('/api/execute', function(req, res) {
  const { sessionId = 'default', command, timeout = 30 } = req.body;
  
  if (!command) {
    res.status(400).json({ success: false, error: 'Command is required' });
    return;
  }
  
  try {
    // Get or create session
    const session = getOrCreateSession(sessionId);
    
    if (!session.terminal || session.terminal.killed) {
      res.status(500).json({ 
        success: false, 
        error: 'Terminal session not available',
        stdout: '',
        stderr: 'Terminal session not available',
        exit_code: -1
      });
      return;
    }
    
    // Set up command execution
    let commandOutput = [];
    let hasOutput = false;
    let responseTimeout;
    const startTime = Date.now();
    
    // Function to send response
    const sendResponse = () => {
      if (res.headersSent) return; // Already sent
      
      const elapsed = Date.now() - startTime;
      const timedOut = elapsed >= (timeout * 1000);
      
      // Prepare response
      const stdout = commandOutput
        .filter(item => item.type === 'stdout')
        .map(item => item.data)
        .join('');
      
      const stderr = commandOutput
        .filter(item => item.type === 'stderr')
        .map(item => item.data)
        .join('');
      
      const result = {
        success: hasOutput || !timedOut,
        stdout: stdout,
        stderr: stderr,
        exit_code: timedOut ? 124 : 0,
        command: command,
        timeout: timedOut,
        execution_time: elapsed / 1000
              };
        
        res.json(result);
      };
      
            // Create temporary listener for this command
      const dataListener = (data) => {
        const output = data.toString();
        commandOutput.push({ type: 'stdout', data: output });
        hasOutput = true;
        
        // Reset timeout when we get output
        if (responseTimeout) {
          clearTimeout(responseTimeout);
        }
        responseTimeout = setTimeout(sendResponse, 2000); // Send response 2 seconds after last output
      };
    
    // Add listener
    session.terminal.on('data', dataListener);
    
      // Set overall timeout
      const overallTimeout = setTimeout(() => {
        // Remove listener
        session.terminal.removeListener('data', dataListener);
        
        if (responseTimeout) {
          clearTimeout(responseTimeout);
        }
        
        sendResponse();
      }, timeout * 1000);
      
      // Send command with proper line ending
      session.terminal.write(command + '\n');
      
      // Start initial response timer (in case command produces no output)
      responseTimeout = setTimeout(() => {
        // Remove listener
        session.terminal.removeListener('data', dataListener);
        
        if (overallTimeout) {
          clearTimeout(overallTimeout);
        }
        
        sendResponse();
      }, 5000); // Wait 5 seconds for initial output
      
    } catch (error) {
      res.status(500).json({
        success: false,
        error: error.message,
        stdout: '',
        stderr: error.message,
        exit_code: -1,
        command: command
      });
    }
});

// API endpoint to delete a session
app.delete('/api/sessions/:sessionId', function(req, res) {
  const sessionId = req.params.sessionId;
  
  if (sessionId === 'default') {
    res.status(400).json({ success: false, error: 'Cannot delete default session' });
    return;
  }
  
  if (terminalSessions.has(sessionId)) {
    destroySession(sessionId);
    res.json({ success: true, message: `Session ${sessionId} destroyed` });
  } else {
    res.status(404).json({ success: false, error: 'Session not found' });
  }
});

/* Redirect the rest to index */
app.get('*', function(req, res) {
  res.redirect('/');
});

io.on('connection', (socket) => {
  let currentSessionId = null;

  // Auto-join default session after a brief delay
  setTimeout(() => {
    if (!currentSessionId) {
      joinSessionInternal(socket, 'default');
    }
  }, 1000);

  function joinSessionInternal(socket, sessionId, cwd = null) {
    // Leave current session if any
    if (currentSessionId) {
      removeClientFromSession(currentSessionId, socket);
    }
    
    // Join new session
    currentSessionId = sessionId;
    const session = getOrCreateSession(sessionId, cwd);
    addClientToSession(sessionId, socket);
    
    // Send session info to client
    socket.emit('session-joined', {
      sessionId: sessionId,
      createdAt: session.createdAt,
      lastActivity: session.lastActivity
    });
    
    // Send recent output history to new client
    const recentHistory = session.outputHistory.slice(-20); // Last 20 entries
    recentHistory.forEach(entry => {
      if (entry.type === 'stdout') {
        socket.emit('terminal-output-stdout', { message: entry.data });
      } else if (entry.type === 'stderr') {
        socket.emit('terminal-output-stderr', { message: entry.data });
      }
    });
  }

  // Handle joining a specific session
  socket.on('join-session', (data) => {
    const sessionId = data.sessionId || 'default';
    const cwd = data.cwd;
    joinSessionInternal(socket, sessionId, cwd);
  });

  // Handle terminal resize
  socket.on('resize', (data) => {
    if (currentSessionId) {
      const session = terminalSessions.get(currentSessionId);
      if (session && session.terminal && !session.terminal.killed) {
        try {
          session.terminal.resize(data.cols || 120, data.rows || 30);
        } catch (error) {
          // Ignore resize errors
        }
      }
    }
  });

  // Handle command input
  socket.on('input', (command) => {
    if (!currentSessionId) {
      // Auto-join default session if not already in one
      joinSessionInternal(socket, 'default');
    }
    
    const session = terminalSessions.get(currentSessionId);
    if (session && session.terminal && !session.terminal.killed) {
      try {
        // Send command as-is to the terminal
        session.terminal.write(command);
        session.lastActivity = Date.now();
      } catch (error) {
        socket.emit('terminal-output-stdout', { 
          message: `Error: Terminal session not available\n` 
        });
      }
    } else {
      socket.emit('terminal-output-stdout', { 
        message: `Error: No active terminal session\n` 
      });
    }
  });

  // Handle disconnect
  socket.on('disconnect', () => {
    if (currentSessionId) {
      removeClientFromSession(currentSessionId, socket);
    }
  });

  // Handle connection errors
  socket.on('error', (error) => {
    // TODO: Graceful error handling
    console.error(`Socket error for client ${socket.id}:`, error);
  });
});

// Graceful shutdown
function gracefulShutdown() {
  console.log('\nShutting down gracefully...');
  // Close all terminal sessions
  for (const [sessionId, session] of terminalSessions.entries()) {
    if (session.terminal && !session.terminal.killed) {
      console.log(`Killing terminal for session: ${sessionId}`);
      try {
        session.terminal.kill('SIGKILL');
      } catch (e) {
        console.error(`Failed to kill terminal for session ${sessionId}:`, e);
      }
    }
  }
  
  // Close the HTTP server
  http.close(() => {
    console.log('Server and all connections closed.');
    process.exit(0);
  });

  // Force exit after a timeout
  setTimeout(() => {
    console.error('Could not close connections in time, forcing shutdown.');
    process.exit(1);
  }, 10000);
}

process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

const PORT = process.env.PORT || 3000;
http.listen(PORT, () => {
  console.log(`Terminal session server started on port ${PORT}`);
  
  // Create default session after server starts
  setTimeout(createDefaultSession, 1000);
});
