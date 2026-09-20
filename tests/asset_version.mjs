import {readFileSync} from 'node:fs';

const version = readFileSync(new URL('../app_version.py', import.meta.url), 'utf8')
  .match(/^APP_VERSION = "([0-9.]+)"/m)?.[1];
if (!version) throw new Error('APP_VERSION is missing');
export function assetVersionPattern(asset) {
  const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(escape(asset) + '\\?v=' + escape(version));
}
