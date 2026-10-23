/** Actual site's strict parser, controlled download contract only; no external calls. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
globalThis.fetch = async () => { throw new Error('Unexpected network'); };
const {parseSelection, parseDescriptor, PUBLIC_FILES} = await import(pathToFileURL(join(process.env.REEP_SITE_ROOT!, 'src/lib/release-selection.ts')).href);
const input = JSON.parse(readFileSync(0,'utf8'));
const selected = parseSelection(Buffer.from(JSON.stringify(input.selection)), 'downloads');
const descriptor = parseDescriptor(Buffer.from(JSON.stringify(input.descriptor)),selected);
console.log(JSON.stringify({channel:descriptor.channel,stamp:descriptor.stamp,files:PUBLIC_FILES}));
