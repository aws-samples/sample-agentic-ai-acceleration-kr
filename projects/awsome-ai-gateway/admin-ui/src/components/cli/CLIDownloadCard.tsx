'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { Monitor, Laptop, Terminal, Download, Copy, Check } from 'lucide-react';
import type { CLIDownloadItem } from '@/types/entities';

interface CLIDownloadCardProps {
  item: CLIDownloadItem;
}

function getOSIcon(os: string) {
  const lower = os.toLowerCase();
  if (lower === 'darwin') return <Laptop size={24} aria-hidden="true" />;
  if (lower === 'windows') return <Monitor size={24} aria-hidden="true" />;
  // linux 및 기타
  return <Terminal size={24} aria-hidden="true" />;
}

function formatFileSize(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function truncateChecksum(checksum: string): string {
  return checksum.length > 16 ? `${checksum.slice(0, 8)}...${checksum.slice(-8)}` : checksum;
}

function getOSDisplayName(os: string): string {
  const map: Record<string, string> = { linux: 'Linux', darwin: 'macOS', windows: 'Windows' };
  return map[os.toLowerCase()] ?? os;
}

function getCurlCommand(item: CLIDownloadItem): string | null {
  if (item.os === 'windows') return null;
  const base = '$GATEWAY_ADMIN_URL';
  const extractDir = `gateway-cli-${item.version}`;
  return `curl -fsSL ${base}/cli/download/${item.os}/${item.arch} -o ${item.filename} \\\n  && tar -xzf ${item.filename} \\\n  && cd ${extractDir} \\\n  && ./install.sh \\\n  && gateway-cli login \\\n  && gateway-cli setup --gateway-url "$GATEWAY_URL"`;
}

export function CLIDownloadCard({ item }: CLIDownloadCardProps) {
  const [copied, setCopied] = useState(false);
  const [cmdCopied, setCmdCopied] = useState(false);

  const curlCmd = getCurlCommand(item);

  async function handleCopyChecksum() {
    try {
      await navigator.clipboard.writeText(item.checksum_sha256);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard API 미지원 환경 무시
    }
  }

  async function handleCopyCurl() {
    if (!curlCmd) return;
    try {
      await navigator.clipboard.writeText(curlCmd);
      setCmdCopied(true);
      setTimeout(() => setCmdCopied(false), 2000);
    } catch {}
  }

  return (
    <div className="glass glass-hover rounded-apple p-4 flex flex-col gap-4">
      {/* OS 아이콘 + OS/아키텍처 */}
      <div className="flex items-center gap-3">
        <div className="text-muted-foreground">{getOSIcon(item.os)}</div>
        <div>
          <p className="font-bold text-sm">
            {getOSDisplayName(item.os)} ({item.arch})
          </p>
          <p className="text-xs text-muted-foreground">버전: v{item.version}</p>
        </div>
      </div>

      {/* 파일 정보 */}
      <div className="flex flex-col gap-1 text-xs text-muted-foreground">
        <div className="flex justify-between">
          <span>파일 크기</span>
          <span className="font-medium text-foreground">
            {formatFileSize(item.file_size_bytes)}
          </span>
        </div>
        <div className="flex justify-between items-center">
          <span>SHA-256</span>
          <div className="flex items-center gap-1">
            <span
              className="font-mono text-foreground"
              title={item.checksum_sha256}
            >
              {truncateChecksum(item.checksum_sha256)}
            </span>
            <button
              onClick={handleCopyChecksum}
              className="rounded-sm p-0.5 hover:bg-muted transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              aria-label="체크섬 복사"
            >
              {copied ? (
                <Check size={12} className="text-green-600" aria-hidden="true" />
              ) : (
                <Copy size={12} aria-hidden="true" />
              )}
            </button>
          </div>
        </div>
      </div>

      {/* 다운로드 버튼 */}
      <a
        href={item.download_url}
        download={item.filename}
        className={[
          'inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors',
          'bg-primary text-primary-foreground shadow hover:bg-primary/90',
          'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
        ].join(' ')}
      >
        <Download size={14} aria-hidden="true" />
        다운로드
      </a>

      {/* CLI 설치 명령어 (Linux/macOS only) */}
      {curlCmd && (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">CLI로 설치</span>
            <button
              onClick={handleCopyCurl}
              className="rounded-sm p-0.5 hover:bg-muted transition-colors text-xs text-muted-foreground flex items-center gap-1"
              aria-label="명령어 복사"
            >
              {cmdCopied ? (
                <><Check size={12} className="text-green-600" /> 복사됨</>
              ) : (
                <><Copy size={12} /> 복사</>
              )}
            </button>
          </div>
          <pre className="bg-muted rounded p-2 text-xs font-mono overflow-x-auto whitespace-pre-wrap break-all">
            {curlCmd}
          </pre>
        </div>
      )}
    </div>
  );
}