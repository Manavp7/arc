import test from 'node:test';
import assert from 'node:assert/strict';
import { initialMovement, movementKey, movementProblem, reverseCountingLine, countingLineLabels, busiestSamples, movementTime } from '../src/lib/movement.ts';

test('occupancy-only configuration needs neither a line nor a confidence score', () => {
  assert.equal(movementProblem(initialMovement(), []), null);
  assert.equal(movementProblem({ ...initialMovement(), min_confidence: 0 }, []), null);
});
test('persisted report and calculated summary match despite JSON key ordering', () => {
  const line = { name: 'Gate', start: [.2, .5], end: [.8, .5] };
  const client = { line, class_name: 'person', min_confidence: .5, zone_id: 'gate' };
  const server = { zone_id: 'gate', min_confidence: .5, class_name: 'person', line: { end: line.end, start: line.start, name: line.name } };
  assert.equal(movementKey(client), movementKey(server));
  assert.notEqual(movementKey(client), movementKey({ ...server, min_confidence: null }));
});
test('draft validation rejects blank classes, unknown zones and incomplete confidence', () => {
  assert.match(movementProblem({ ...initialMovement(), class_name: ' ' }, []), /object class/);
  assert.match(movementProblem({ ...initialMovement(), zone_id: 'missing' }, []), /retained/);
  for (const min_confidence of [NaN, Infinity, -.1, 1.1]) assert.match(movementProblem({ ...initialMovement(), min_confidence }, []), /confidence/);
  assert.notEqual(movementKey(initialMovement()), movementKey({ ...initialMovement(), min_confidence: NaN }));
});
test('finite counting line validation prevents accidental points and off-frame coordinates', () => {
  const base = { ...initialMovement(), line: { name: 'Entrance', start: [.5, .1], end: [.5, .9] } };
  assert.equal(movementProblem(base, []), null);
  assert.match(movementProblem({ ...base, line: { ...base.line, end: [.5, .10001] } }, []), /farther apart/);
  for (const start of [[-1, 0], [1.1, 0], [NaN, .5]]) assert.match(movementProblem({ ...base, line: { ...base.line, start } }, []), /coordinates/);
});
test('reversing line swaps screen-left A and screen-right B without mutating the original', () => {
  const line = { name: 'Gate', start: [.2, .5], end: [.8, .5] };
  const labels = countingLineLabels(line), reversed = reverseCountingLine(line);
  assert.ok(labels.a[1] < .5); assert.ok(labels.b[1] > .5);
  assert.deepEqual(countingLineLabels(reversed), { a: labels.b, b: labels.a });
  assert.deepEqual(line.start, [.2, .5]); assert.notEqual(line.end, reversed.start);
  const down = countingLineLabels({ name: 'Down', start: [.5, .2], end: [.5, .8] });
  assert.ok(down.a[0] > .5); assert.ok(down.b[0] < .5);
});
test('busiest samples are ranked observations with stable time ties, not summed people', () => {
  const samples = [{ at_s: 2, count: 3 }, { at_s: 1, count: 3 }, { at_s: 0, count: 0 }, { at_s: 4, count: 2 }];
  assert.deepEqual(busiestSamples(samples, 2), [samples[1], samples[0]]);
  assert.deepEqual(samples.map(sample => sample.at_s), [2, 1, 0, 4]);
  assert.deepEqual(busiestSamples([{ at_s: 0, count: 0 }]), []);
  assert.equal(movementTime(62.5), '1:02.50');
});
