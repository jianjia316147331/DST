import type { TaskProgress } from './types.js';

interface ParseResult {
  progress?: TaskProgress;
  progressDesc?: string;
  stats?: {
    processedVehicles?: number;
    totalVehicles?: number;
    currentPage?: number;
    violationsFound?: number;
  };
}

// Keywords that signal progress transitions
const PROGRESS_KEYWORDS: { pattern: RegExp; progress: TaskProgress }[] = [
  // Entry navigation
  { pattern: /(init|初始化|创建目录|违章查询\/)/i, progress: '入口导航' },
  { pattern: /(pinchtab\s+navigate|导航到.*122\.gov\.cn|navigate.*122)/i, progress: '入口导航' },
  // Login
  { pattern: /(login|登录|二维码|qrcode|扫码)/i, progress: '登录中' },
  { pattern: /(upload-image|gen-qr-msg|send-msg.*qr)/i, progress: '登录中' },
  // Query prep (login success)
  { pattern: /(poll-login.*success|登录成功|单位用户|logged\sin|单位用户登录)/i, progress: '查询准备' },
  { pattern: /(公司列表|company.*select|选择公司)/i, progress: '查询准备' },
  // Querying
  { pattern: /(list-vehicles|get-page-vehicles|collect-violations|open-vehicle)/i, progress: '查询中' },
  { pattern: /(正在采集|正在查询|翻至第\d+页)/i, progress: '查询中' },
  // Completed
  { pattern: /(查询完成|汇总|报告|violations\.db|查询结束)/i, progress: '已完成' },
];

// Extract stats from output lines
function extractStats(line: string): ParseResult['stats'] {
  const stats: NonNullable<ParseResult['stats']> = {};

  const processed = line.match(/(?:已处理|processed)[:：\s]*(\d+)/i);
  if (processed) stats.processedVehicles = parseInt(processed[1]);

  const total = line.match(/(?:总数|total)[:：\s]*(\d+)/i);
  if (total) stats.totalVehicles = parseInt(total[1]);

  const page = line.match(/第\s*(\d+)\s*页/);
  if (page) stats.currentPage = parseInt(page[1]);

  const violations = line.match(/(?:违章|违法|violations)[:：\s]*(\d+)\s*条/i);
  if (violations) stats.violationsFound = parseInt(violations[1]);

  return Object.keys(stats).length > 0 ? stats : undefined;
}

// Extract human-readable progress description
function extractProgressDesc(line: string): string | undefined {
  const descPatterns = [
    /(正在导航.*?\.{3})/,
    /(二维码已生成.*)/,
    /(等待.*扫码)/,
    /(登录成功.*)/,
    /(正在进入.*)/,
    /(已选择公司.*)/,
    /(正在翻至第\d+页.*)/,
    /(正在采集.*违章.*)/,
    /(已采集.*罚款.*记分.*)/,
    /(查询完成.*)/,
  ];

  for (const p of descPatterns) {
    const m = line.match(p);
    if (m) return m[1];
  }
  return undefined;
}

export function parseLine(line: string): ParseResult {
  const result: ParseResult = {};

  // Check progress transitions (in reverse order so more specific patterns win)
  for (const { pattern, progress } of [...PROGRESS_KEYWORDS].reverse()) {
    if (pattern.test(line)) {
      result.progress = progress;
      break;
    }
  }

  const desc = extractProgressDesc(line);
  if (desc) result.progressDesc = desc;

  const stats = extractStats(line);
  if (stats) result.stats = stats;

  return result;
}

// Generate compact progress summary for progress_desc field
export function generateProgressDesc(line: string): string {
  // Truncate to max 200 chars for DB storage
  const trimmed = line.replace(/^[\s$>]+/, '').trim();
  return trimmed.length > 200 ? trimmed.slice(0, 197) + '...' : trimmed;
}
