"use client";

import { METRIC_COLOR } from "@/app/components/chartTheme";
import type { Composition, CompositionEntry, ToolUsage } from "@/lib/insights";
import { topNWithOther } from "@/app/insights/insightsFormat.mjs";
import { Plot, PlotCell, PlotGrid, RankedBars, type RankedEntry } from "./charts";

/**
 * Traffic that flowed through a shared component, as bars.
 *
 * Colour follows the page rule: same nature, same hue. The MCP-server, skill and
 * knowledge-base lists all measure the same quantity — turns that reached the
 * component — so they carry the turns hue (`METRIC_COLOR.turns`), the same blue a
 * turn count wears everywhere else. They are not four different measures dressed in
 * four colours; their heading and the rule between them say which component each is.
 * Tool calls is the exception because it is a different quantity (call count, not
 * turns), so it takes the tool-call hue — the same orange as the tool-call metric.
 */
const byTraffic = (entries: CompositionEntry[]) =>
  topNWithOther(
    entries.map((entry) => ({
      name: entry.name,
      value: entry.turns,
      meta: `${entry.agents}개 에이전트`,
    })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: `${entry.value.toLocaleString("ko-KR")}턴`,
  })) as RankedEntry[];

export function ReusePanel({ composition }: { composition: Composition }) {
  const tools = topNWithOther(
    composition.tools.map((tool: ToolUsage) => ({
      name: tool.name,
      value: tool.tool_calls,
    })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: `${entry.value.toLocaleString("ko-KR")}회`,
  })) as RankedEntry[];

  return (
    <PlotGrid>
      <PlotCell>
        <Plot title="툴 호출" hint="실제 호출 수입니다.">
          <RankedBars
            entries={tools}
            colorIndex={METRIC_COLOR.tool_calls}
            empty="이 기간에 호출된 툴이 없습니다."
          />
        </Plot>
      </PlotCell>
      <PlotCell>
        <Plot title="MCP 서버" hint="이 MCP 서버를 붙인 에이전트가 처리한 턴 수입니다.">
          <RankedBars entries={byTraffic(composition.mcp)} colorIndex={METRIC_COLOR.turns} />
        </Plot>
      </PlotCell>
      <PlotCell>
        <Plot
          title="스킬 도달"
          hint="이 스킬을 붙인 에이전트가 처리한 턴 수입니다 (스킬은 호출 카운트가 없어 턴으로 셉니다)."
        >
          <RankedBars entries={byTraffic(composition.skills)} colorIndex={METRIC_COLOR.turns} />
        </Plot>
      </PlotCell>
      <PlotCell>
        <Plot
          title="지식 베이스"
          hint="이 지식 베이스를 붙인 에이전트가 처리한 턴 수입니다."
        >
          <RankedBars entries={byTraffic(composition.knowledge_bases)} colorIndex={METRIC_COLOR.turns} />
        </Plot>
      </PlotCell>
    </PlotGrid>
  );
}
