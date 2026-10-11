import {createCipheriv, createDecipheriv, createHash, randomBytes} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync, renameSync, existsSync} from 'node:fs';
import {dirname} from 'node:path';

export class EncryptedStore {
  constructor(secret, replacer, reviver) {
    if (!secret) throw new Error('WhatsApp storage key is required');
    this.key = createHash('sha256').update(secret).digest();
    this.replacer = replacer;
    this.reviver = reviver;
  }
  read(path, fallback) {
    if (!existsSync(path)) return fallback;
    const bytes = readFileSync(path);
    const decipher = createDecipheriv('aes-256-gcm', this.key, bytes.subarray(0, 12));
    decipher.setAuthTag(bytes.subarray(12, 28));
    return JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString(), this.reviver);
  }
  write(path, value) {
    mkdirSync(dirname(path), {recursive: true, mode: 0o700});
    const nonce = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.key, nonce);
    const body = Buffer.concat([cipher.update(JSON.stringify(value, this.replacer)), cipher.final()]);
    const temporary = path + '.tmp';
    writeFileSync(temporary, Buffer.concat([nonce, cipher.getAuthTag(), body]), {mode: 0o600});
    renameSync(temporary, path);
  }
}
