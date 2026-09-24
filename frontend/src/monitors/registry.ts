import type {MonitorSummary, MonitorObservation, MonitoredPackage, Target} from '../lib/api.generated';
import Source from './Source.astro';
import Version from './Version.astro';
import Build from './Build.astro';
import Evidence from './Evidence.astro';
import Security from './Security.astro';

export interface Props {
  result: MonitorSummary | MonitorObservation;
  pkg: MonitoredPackage;
  filters?: URLSearchParams;
  target?: Target;
  detail?: boolean;
  focused?: boolean;
}

// The page composes slots; renderers format typed data, never decide domain state.
const renderers = {
  source: {Summary: Source, Detail: Source, placement: 'identity', context: []},
  version: {Summary: Version, Detail: Version, placement: 'column', context: []},
  build: {Summary: Build, Detail: Build, placement: 'targets', context: []},
  evidence: {Summary: Evidence, Detail: Evidence, placement: 'signals', context: []},
} as const;
const specializations = new Map<string, Omit<typeof renderers.evidence, 'context'> & {context: readonly string[]}>([
  ['security', {...renderers.evidence, Summary: Security, Detail: Security}],
  ['license', {...renderers.evidence, context: ['version']}],
]);

export function renderer(monitor: {id: string; kind: keyof typeof renderers}) {
  return monitor.kind === 'evidence'
    ? specializations.get(monitor.id) ?? renderers.evidence : renderers[monitor.kind];
}
