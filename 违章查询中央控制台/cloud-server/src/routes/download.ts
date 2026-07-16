import { FastifyInstance } from 'fastify';
import { createReadStream } from 'fs';
import { statSync, readdirSync } from 'fs';
import { join } from 'path';

const RELEASE_DIR = join(import.meta.dirname, '..', '..', '..', 'tray-app', 'release');

const MIME: Record<string, string> = {
  '.dmg': 'application/x-apple-diskimage',
  '.exe': 'application/vnd.microsoft.portable-executable',
  '.zip': 'application/zip',
};

function findLatest(pattern: string): string | null {
  try {
    const files = readdirSync(RELEASE_DIR)
      .filter((f) => f.includes(pattern) && !f.endsWith('.blockmap') && !f.endsWith('.yml'))
      .sort()
      .reverse();
    return files.length > 0 ? files[0] : null;
  } catch {
    return null;
  }
}

export default async function downloadRoutes(app: FastifyInstance) {
  app.get('/api/tray-app/download', async (request, reply) => {
    const { platform } = request.query as { platform?: string };

    // Respect installers built for the requesting platform
    const ext = platform === 'win' ? '.exe' : '.dmg';
    const keyword = platform === 'win' ? 'Setup' : '';
    const filename = findLatest(keyword + ext) || findLatest(ext);

    if (!filename) {
      return reply.status(404).send({
        error: 'NO_PACKAGE',
        message: `${platform === 'win' ? 'Windows' : 'macOS'} 安装包尚未构建。请在 ${platform === 'win' ? 'Windows' : 'macOS'} 机器上运行 npm run pack:${platform === 'win' ? 'win' : 'mac'} 生成安装包。`,
      });
    }

    const filepath = join(RELEASE_DIR, filename);
    const stat = statSync(filepath);

    reply.header('Content-Disposition', `attachment; filename="${encodeURIComponent(filename)}"`);
    reply.header('Content-Type', MIME[ext] || 'application/octet-stream');
    reply.header('Content-Length', stat.size);
    return reply.send(createReadStream(filepath));
  });
}
