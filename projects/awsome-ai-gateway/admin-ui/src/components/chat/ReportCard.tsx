// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useState } from 'react';
import { FileText, FileSpreadsheet, Presentation, Download, Loader2 } from 'lucide-react';
import type { ReportFile } from './types';

const API_BASE = '/api/chat-proxy';

const FORMAT_ICON: Record<string, typeof FileText> = {
  pdf: FileText,
  xlsx: FileSpreadsheet,
  pptx: Presentation,
};

/**
 * 다운로드 리포트 카드. report 이벤트로 받은 s3_uri 를 클릭 시점에 presign 요청
 * (URL 을 미리 굽지 않음 — 만료 통제·검증 우회 방지). admin-api 가 prefix/버킷
 * 화이트리스트 + 5분 만료 presigned URL 을 발급하면 새 탭으로 다운로드.
 */
export function ReportCard({ report }: { report: ReportFile }) {
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const Icon = FORMAT_ICON[report.format] || FileText;

  async function handleDownload() {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetch(
        `${API_BASE}/admin/chat/reports/download?uri=${encodeURIComponent(report.s3_uri)}`,
        { credentials: 'include' }
      );
      if (!res.ok) {
        throw new Error(res.status === 404 ? '리포트를 찾을 수 없습니다(만료되었을 수 있음).' : `다운로드 실패 (${res.status})`);
      }
      const data = (await res.json()) as { download_url: string };
      // presigned URL 로 다운로드 트리거(새 탭 — Content-Disposition: attachment).
      window.open(data.download_url, '_blank', 'noopener,noreferrer');
    } catch (e) {
      setErr(e instanceof Error ? e.message : '다운로드 중 오류');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-3 flex items-center gap-3 rounded-lg border border-border bg-gradient-to-br from-primary/5 to-transparent px-4 py-3">
      <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon size={20} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">{report.file_name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {report.summary}
          {report.page_count ? ` · ${report.page_count}p` : ''}
        </div>
        {err && <div className="mt-1 text-xs text-destructive">{err}</div>}
      </div>
      <button
        type="button"
        onClick={handleDownload}
        disabled={loading}
        className="flex flex-shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
      >
        {loading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
        {loading ? '준비 중…' : '다운로드'}
      </button>
    </div>
  );
}
