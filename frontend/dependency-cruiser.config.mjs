export default {
  forbidden: [
    {name: 'no-cycles', severity: 'error', from: {}, to: {circular: true}},
    {name: 'shared-primitives-do-not-import-views', severity: 'error',
      from: {path: '^src/lib/'}, to: {path: '^src/(pages|components|layouts|scripts)/'}},
    {name: 'browser-code-does-not-import-server-client', severity: 'error',
      from: {path: '^src/scripts/'}, to: {path: '^src/lib/api\\.ts$'}},
  ],
  options: {
    doNotFollow: {path: 'node_modules'},
    tsConfig: {fileName: 'tsconfig.json'},
    tsPreCompilationDeps: true,
  },
};
