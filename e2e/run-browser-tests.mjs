import { createServer } from 'node:net';
import { spawn } from 'node:child_process';

const portServer = createServer();
portServer.listen(0, '127.0.0.1');
await new Promise((resolve) => portServer.once('listening', resolve));
const address = portServer.address();
if (!address || typeof address === 'string') throw new Error('Could not allocate browser-test port');
await new Promise((resolve, reject) => portServer.close((error) => error ? reject(error) : resolve()));

const playwright = spawn('npx', ['playwright', 'test'], {
  env: { ...process.env, VTA_BROWSER_PORT: String(address.port) },
  stdio: 'inherit',
});
playwright.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exitCode = code ?? 1;
});
