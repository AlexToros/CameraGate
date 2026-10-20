const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const scope={window:{},document:{addEventListener(){}}};vm.createContext(scope);vm.runInContext(fs.readFileSync('frontend/player.js','utf8'),scope);
const Policy=scope.window.CameraStream.BufferPolicy,policy=new Policy();
const start=policy.lastStall;
assert.equal(policy.ready(.01,start),true); // no startup accumulation, even one frame is enough
assert.equal(policy.position(0,2,0,false),1.95);
assert.equal(policy.position(0,.03,0,false),0);
assert.equal(policy.position(0,2,0,true),null); // ordinary buffering must not seek away frames
policy.stall(start+1000);assert.ok(Math.abs(policy.target-.9)<1e-8);
assert.equal(policy.ready(.4,start+1100),false);assert.equal(policy.recovering,true);
assert.equal(policy.ready(1,start+2000),true);assert.equal(policy.recovering,false);
for(let i=0;i<8;i++){policy.stall(start+3000+i);policy.ready(2,start+3000+i);}
assert.equal(policy.target,1.5);assert.equal(policy.position(0,4,0,true),2.5);
assert.equal(policy.position(0,2,0,true),null);
policy.ready(1.5,start+50000);assert.ok(policy.target<1.5 && policy.target>=.6);
assert.equal(policy.position(5,6,0,true),5);
console.log('PASS bounded adaptive buffer, hold on starvation, resume after refill, stable playback without repeated catch-up seeks, safe catch-up and decay');
