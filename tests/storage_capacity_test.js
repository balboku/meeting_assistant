'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
vm.runInThisContext(html.match(/function formatBytes\(bytes = 0\) \{[\s\S]*?\n\}/)[0]);
assert.equal(formatBytes(1024), '1 KiB');
assert.equal(formatBytes(1024 ** 2), '1 MiB');
assert.equal(formatBytes(1024 ** 3), '1.0 GiB');
assert.equal(formatBytes(19100000000), '17.8 GiB');
assert.equal(formatBytes(0), '0 B');
const elements = new Map();
function element(tag) {
  return { tag, children: [], classList: { toggle() {} }, append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener(type, callback) { this[type] = callback; },
    showModal() { this.open = true; }, close() { this.open = false; } };
}
elements.set('ops-backup-storage', element('span'));
elements.set('ops-backup-storage-tile', element('button'));
global.document = { getElementById: id => elements.get(id), createElement: element,
  body: { append(node) { elements.set(node.id, node); } } };
global.window = {};
require('../static/storage-capacity.js');
const view = window.StorageCapacity;
view.render({ backup_storage: { complete: true, total_bytes: 2 * 1024 ** 3, categories: { snapshots: 1024 ** 3, shared_media: 1024 ** 3 } } });
assert.equal(elements.get('ops-backup-storage').textContent, '2.0 GiB');
view.open();
const dialog = elements.get('backup-capacity-dialog');
assert.equal(dialog.open, true);
assert.ok(dialog.children.some(c => (c.textContent || '').includes('bytes')));
dialog.children.at(-1).click();
assert.equal(dialog.open, false);
view.render({ backup_storage: { complete: false, total_bytes: 0 } });
assert.equal(elements.get('ops-backup-storage').textContent, '未取得');
view.open();
assert.ok(dialog.children.some(c => (c.textContent || '').includes('重新整理')));
view.render(null);
assert.equal(elements.get('ops-backup-storage').textContent, '未取得');
view.render({ backup_storage: { complete: true, total_bytes: 0, categories: {} } });
assert.equal(elements.get('ops-backup-storage').textContent, '0 B');
console.log('storage capacity JS contracts passed');
