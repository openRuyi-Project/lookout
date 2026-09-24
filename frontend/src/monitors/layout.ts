import type {MonitorDescription} from '../lib/api.generated';
import {renderer} from './registry';

export function tableLayout(monitors: MonitorDescription[], focus: string) {
  const slot = (placement: string) => monitors.filter(m => renderer(m).placement === placement);
  const selected = monitors.find(m => m.id === focus);
  const context = selected ? monitors.filter(m => (renderer(selected).context as readonly string[]).includes(m.id)) : [];
  return {
    identity: slot('identity').filter(m => m !== selected),
    signals: selected ? [] : slot('signals'),
    columns: selected ? (selected.kind === 'build' ? [] : [...context, selected]) : slot('column'),
    targets: selected ? (selected.kind === 'build' ? [selected] : []) : slot('targets'),
  };
}

export function detailLayout(monitors: MonitorDescription[]) {
  const order = {column: 0, signals: 1, targets: 2, identity: 3};
  return [...monitors].sort((a, b) => order[renderer(a).placement] - order[renderer(b).placement]);
}
