import {extname} from 'node:path';

const zip = b => [Buffer.from([80,75,3,4]), Buffer.from([80,75,5,6]), Buffer.from([80,75,7,8])].some(h => b.subarray(0,4).equals(h));
const ole = b => b.subarray(0,8).equals(Buffer.from([208,207,17,224,161,177,26,225]));
const text = b => !b.subarray(0,32).includes(0);
const documents = {
  '.pdf': ['application/pdf', b => b.subarray(0,5).toString() === '%PDF-'],
  '.doc': ['application/msword', ole],
  '.docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', zip],
  '.xls': ['application/vnd.ms-excel', ole],
  '.xlsx': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', zip],
  '.ppt': ['application/vnd.ms-powerpoint', ole],
  '.pptx': ['application/vnd.openxmlformats-officedocument.presentationml.presentation', zip],
  '.txt': ['text/plain', text], '.csv': ['text/csv', text],
  '.rtf': ['application/rtf', b => b.subarray(0,32).toString().trimStart().startsWith('{\\rtf')],
  '.odt': ['application/vnd.oasis.opendocument.text', zip],
  '.ods': ['application/vnd.oasis.opendocument.spreadsheet', zip],
  '.odp': ['application/vnd.oasis.opendocument.presentation', zip],
  '.zip': ['application/zip', zip],
};

export function mediaKind(media) {
  const b = media.bytes;
  if (!b?.length || b.length > 50 * 1024 * 1024 || Buffer.byteLength(media.filename || '') > 255) return null;
  const document = documents[extname(media.filename || '').toLowerCase()];
  if (document && document[0] === media.mime && document[1](b)) return 'document';
  if (media.mime === 'video/mp4' && b.subarray(4,8).toString() === 'ftyp') return 'video';
  if (b.length > 16 * 1024 * 1024) return null;
  const images = {
    'image/png': () => b.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])),
    'image/jpeg': () => b[0] === 255 && b[1] === 216 && b[2] === 255,
    'image/webp': () => b.subarray(0,4).toString() === 'RIFF' && b.subarray(8,12).toString() === 'WEBP',
  };
  return images[media.mime]?.() ? 'image' : null;
}
