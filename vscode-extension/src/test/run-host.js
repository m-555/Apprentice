// Real VS Code extension-host test; isolated profile/repo + fake HTTP model, no GPU.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'apprentice-host-'));
const repo = path.join(root, 'fixture');
fs.mkdirSync(repo);
fs.writeFileSync(path.join(repo, 'calc.py'), 'def add(a, b):\n    return a - b\n');
fs.writeFileSync(path.join(repo, '.gitignore'), '__pycache__/\n.vscode/\n');
for (const args of [['init'], ['add', '-A'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture']]) {
  execFileSync('git', ['-C', repo, ...args], { windowsHide: true, stdio: 'pipe' });
}
const extension = path.resolve(__dirname, '../..');
const checkout = path.dirname(extension);
const python = process.env.APPRENTICE_TEST_PYTHON || path.join(checkout, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
fs.mkdirSync(path.join(repo, '.vscode'));
fs.writeFileSync(path.join(repo, '.vscode/settings.json'), JSON.stringify({
  'apprentice.pythonPath': python, 'apprentice.repoPath': checkout,
  'apprentice.provider': 'fixture', 'apprentice.verify': 'tests',
  'apprentice.testCommand': `"${python}" -c "from calc import add; assert add(2,3) == 5"`,
}));
const env = { ...process.env, APPRENTICE_EXTENSION_TEST_MODE: '1', APPRENTICE_HOME: path.join(root, 'home'), APPRENTICE_TEST_ROOT: root };
for (const name of ['ELECTRON_RUN_AS_NODE', 'VSCODE_IPC_HOOK_CLI']) delete env[name];
const exe = process.env.APPRENTICE_TEST_VSCODE;
if (!exe) throw new Error('Set APPRENTICE_TEST_VSCODE to the installed Code executable.');
const child = spawn(exe, [repo, '--new-window', '--disable-gpu', '--disable-workspace-trust',
  '--skip-welcome', '--skip-release-notes', '--user-data-dir', path.join(root, 'profile'),
  '--extensions-dir', path.join(root, 'extensions'), `--extensionDevelopmentPath=${extension}`,
  `--extensionTestsPath=${path.join(__dirname, 'extensionHost.js')}`], { env, windowsHide: true, stdio: 'ignore' });
console.log(`Isolated extension-host fixture: ${root}`);
child.on('error', err => { console.error(err); process.exitCode = 1; });
child.on('exit', code => {
  const report = path.join(root, 'result.json');
  const result = fs.existsSync(report) ? JSON.parse(fs.readFileSync(report, 'utf8')) : { passed: false, error: `VS Code exited ${code} without a report` };
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = result.passed ? 0 : 1;
});
