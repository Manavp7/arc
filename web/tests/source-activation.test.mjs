import test from 'node:test';
import assert from 'node:assert/strict';
import {activationAction, activationEndpoint, activationProblem} from '../src/lib/source-activation.ts';

test('source paths encode identifiers without allowing path traversal into different actions', () => {
  assert.equal(activationEndpoint('gate/a?x=1', 'preview'), '/sources/gate%2Fa%3Fx%3D1/activation/preview');
  assert.equal(activationEndpoint('gate-a', 'activate'), '/sources/gate-a/activation');
  assert.equal(activationEndpoint('gate-a', 'rollback'), '/sources/gate-a/activation/rollback');
});
test('disabled configuration cannot be presented as incoming-data verification', () => {
  assert.equal(activationAction({action:'activate', requires_fresh_observation:false}), 'Apply disabled state');
  assert.equal(activationAction({action:'rollback', requires_fresh_observation:false}), 'Restore previous configuration');
});
test('automatic recovery and incomplete rollback stay visible as problems, unlike intentional rollback', () => {
  assert.equal(activationProblem({status:'rolled_back', message:'Activation failed (TimeoutError); previous configuration restored'}), true);
  assert.equal(activationProblem({status:'rollback_failed', message:'Storage failed'}), true);
  assert.equal(activationProblem({status:'rolled_back', message:'Previous source configuration restored'}), false);
  assert.equal(activationProblem({status:'verified'}), false);
});
