/**
 * Barrel of the modules that do NOT import `vscode`, bundled to dist/test-bundle.js so
 * the unit tests can run under plain Node (`node --test`) with no editor host.
 *
 * Keeping these four modules vscode-free is deliberate: process handling, protocol
 * parsing, argument building and executable discovery are the parts most likely to break,
 * and they are exactly the parts that would otherwise need a full Extension Development
 * Host to test.
 */

export * from "./protocol";
export * from "./config";
export * from "./locate";
export * from "./agentProcess";
