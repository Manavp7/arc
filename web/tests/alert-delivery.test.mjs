import test from 'node:test';
import assert from 'node:assert/strict';
import { alertDeliveryApi, canRetryDelivery, deliveryTime } from '../src/lib/alert-delivery.ts';
import { api } from '../src/lib/api.ts';

test('delivery retry requires both server eligibility and integration permission', () => {
  assert.equal(canRetryDelivery({status:'failed',can_retry:true},['integrator']),true);
  assert.equal(canRetryDelivery({status:'blocked',can_retry:true},['admin']),true);
  assert.equal(canRetryDelivery({status:'failed',can_retry:true},['operator']),false);
  assert.equal(canRetryDelivery({status:'failed',can_retry:false},['admin']),false);
  for(const status of ['delivered','pending','sending']) assert.equal(canRetryDelivery({status,can_retry:true},['admin']),false);
});
test('retry sends only the reason, retaining auth and abort semantics in shared client', async () => {
  const original=api.request,calls=[];
  api.request=async (...args)=>{calls.push(args);return {};};
  try {
    const signal=new AbortController().signal;
    await alertDeliveryApi.list('failed',signal);
    await alertDeliveryApi.retry('delivery/one','  receiver repaired  ',signal);
    assert.equal(calls[0][0],'/alert-deliveries?limit=100&status=failed');
    assert.equal(calls[1][0],'/alert-deliveries/delivery%2Fone/retry');
    assert.deepEqual(JSON.parse(calls[1][1].body),{reason:'receiver repaired'});
    assert.equal(calls[1][1].signal,signal);
  } finally { api.request=original; }
});
test('missing or invalid delivery dates never invent a next attempt',()=>{
  assert.equal(deliveryTime(null),'—');assert.equal(deliveryTime('invalid'),'Time unavailable');
});
