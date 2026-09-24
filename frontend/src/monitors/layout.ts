import type {MonitorDescription} from '../lib/api.generated';
import {renderer} from './registry';

export function tableLayout(monitors: MonitorDescription[], focus: string) {
  const slot = (placement: string) => monitors.filter(m => renderer(m).placement === placement);
  const selected = monitors.find(m => m.id === focus);
  return {
    identity: slot('identity').filter(m => m !== selected),
    signals: slot('signals').filter(m => m !== selected),
    columns: selected ? (selected.kind === 'build' ? [] : [selected]) : slot('column'),
    targets: selected ? (selected.kind === 'build' ? [selected] : []) : slot('targets'),
  };
}

export function detailLayout(monitors: MonitorDescription[]) {
  const order = {column: 0, signals: 1, targets: 2, identity: 3};
  return [...monitors].sort((a, b) => order[renderer(a).placement] - order[renderer(b).placement]);
}
