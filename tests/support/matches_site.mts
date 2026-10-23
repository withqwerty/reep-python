/** Actual frozen site handler, synthetic in-memory R2/auth; never contacts a service. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const site = process.env.REEP_SITE_ROOT!;
const { contentKey, digest, splitManifest, parseInventory } = await import(pathToFileURL(join(site, 'scripts/publish-api-generation.ts')).href);
const { handleNextApiRequest } = await import(pathToFileURL(join(site, 'src/lib/next-api.ts')).href);
const legacy = process.env.REEP_TEST_LEGACY === '1';
const request = JSON.parse(readFileSync(0, 'utf8'));
const fixture = JSON.parse(readFileSync(new URL('../fixtures/matches-site.json', import.meta.url), 'utf8'));
const files = fixture.files;
// Synthetic API cadence advances independently from the older download stamp.
files['manifest.json'].release.stamp = '2026-10-23.10';
// The base case is the exact independently read v4-derived one-match fixture.
// The many/changed cases are explicitly synthetic pagination/fault mutations.
if (['many', 'changed'].includes(request.mode)) {
  for (const [path, value] of Object.entries(files) as [string, any][]) {
    if (!path.startsWith('matches/teams/')) continue;
    const original = value.matches[0];
    value.matches = Array.from({length: 103}, (_, i) => ({...original,
      reep_id: `rm${(i+1).toString(16).padStart(14, '0')}`,
      ...(i % 2 ? {home:original.away, away:original.home} : {}),
      played_on: request.mode === 'changed' ? '2026-10-24' : '2026-10-23',
    }));
  }
}
const objects = new Map<string, Buffer>();
const entries = Object.entries(files).map(([path,value]) => {
  const body = Buffer.from(JSON.stringify(value)); const sha256 = digest(body);
  objects.set(contentKey(sha256), body); return {path,bytes:body.length,sha256};
});
const content = {schema_version:1,release_stamp:'2026-10-23.10',
  inputs:[{path:'fixture.json',sha256:digest(JSON.stringify(fixture))}],objects:entries};
const raw = Buffer.from(JSON.stringify({...content,generation_id:digest(JSON.stringify(content))}));
const inventory = parseInventory(raw); const split = splitManifest(inventory,raw);
for (const [key,body] of split.nodes) objects.set(key,body);
const rootRaw = Buffer.from(JSON.stringify(split.manifest));
const reference = {generation_id:inventory.generation_id,
  root:{key:`generations/${inventory.generation_id}/manifest.json`,sha256:digest(rootRaw),bytes:rootRaw.length},inventory:split.manifest.inventory};
objects.set(reference.root.key,rootRaw); objects.set(reference.inventory.key,raw);
const descriptor = {schema:'reep-public-release/v4',channel:'api',stamp:content.release_stamp,
  candidate_sha256:'a'.repeat(64),policy_sha256:'b'.repeat(64),
  source:{generation_id:'synthetic-sdk-fixture',manifest_sha256:'c'.repeat(64),as_of:null},
  generated_at:'2026-10-23T12:01:00Z',source_manifest_sha256:files['manifest.json'].release.source_manifest_sha256,api:reference};
const descriptorRaw = Buffer.from(JSON.stringify(descriptor));
const selection = {schema:'reep-public-selection/v1',channel:'api',stamp:descriptor.stamp,
  candidate_sha256:descriptor.candidate_sha256,policy_sha256:descriptor.policy_sha256,
  generated_at:descriptor.generated_at,activation_requested_at:descriptor.generated_at,predecessor_sha256:null,
  release:{key:`releases/${descriptor.stamp}/api.json`,sha256:digest(descriptorRaw),bytes:descriptorRaw.length}};
objects.set(selection.release.key,descriptorRaw);objects.set('pointer.json',Buffer.from(JSON.stringify(legacy ? {generation_id: inventory.generation_id} : selection)));
// Deliberately older download selection: the API must not read or compare it.
objects.set('releases/latest.json',Buffer.from(JSON.stringify({stamp:'2026-10-01.1'})));
const reads:string[]=[];
const bucket = {async get(key:string) {
  reads.push(key); if(key === 'releases/latest.json') throw new Error('cross-channel read');
  const value=objects.get(key);return value?{size:value.length,arrayBuffer:async()=>Uint8Array.from(value).buffer}:null;
}};
globalThis.fetch = async (input:any) => {
  const url=String(input instanceof Request?input.url:input);
  if(!url.includes('unkey')) throw new Error('unexpected external transport');
  return new Response(JSON.stringify({data:{valid:true}}));
};
const response=await handleNextApiRequest(new Request(request.url,{headers:{Authorization:'Bearer fixture'}}),
  {generationMode:'on',apiSelectionMode:legacy ? undefined : 'on',generationBucket:bucket,unkeyRootKey:'fixture'});
const body = await response.json();
// Explicit transport-corruption negative after the actual handler succeeds.
if (request.mode === 'malformed') { body.results = body.matches; delete body.matches; }
console.log(JSON.stringify({status:response.status,headers:Object.fromEntries(response.headers),body,reads}));
