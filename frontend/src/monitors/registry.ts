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
  source: {Summary: Source, Detail: Source, placement: 'identity'},
  version: {Summary: Version, Detail: Version, placement: 'column'},
  build: {Summary: Build, Detail: Build, placement: 'targets'},
  evidence: {Summary: Evidence, Detail: Evidence, placement: 'signals'},
} as const;
const specializations = new Map<string, typeof renderers.evidence>([
  ['security', {...renderers.evidence, Detail: Security}],
]);

export function renderer(monitor: {id: string; kind: keyof typeof renderers}) {
  return monitor.kind === 'evidence'
    ? specializations.get(monitor.id) ?? renderers.evidence : renderers[monitor.kind];
}
