/**
 * 런타임 클라이언트 설정을 제공합니다.
 *
 * 샌드박스 출처가 런타임에 결정되기 때문에 (ECS 태스크에서 ALB DNS를 받으므로) 빌드 시점 인라인이
 * 불가능합니다. 이 엔드포인트는 브라우저에서 런타임에 조회합니다.
 *
 * 경로: /client-config (/api/* 프록시 Route Handler 밖)
 */
import { NextResponse, type NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  return NextResponse.json({
    sandboxOrigin: process.env.SANDBOX_ORIGIN ?? "",
  });
}
