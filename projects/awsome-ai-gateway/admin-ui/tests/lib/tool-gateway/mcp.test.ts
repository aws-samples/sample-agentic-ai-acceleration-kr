// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { parseToolsResult, SEARCH_META_TOOL } from '@/lib/tool-gateway/mcp';

describe('parseToolsResult', () => {
  it('returns tools and drops the synthetic search meta-tool', () => {
    const tools = parseToolsResult({
      tools: [
        { name: 'serper___web_search', description: 's', inputSchema: {} },
        { name: SEARCH_META_TOOL },
      ],
    });
    expect(tools.map((t) => t.name)).toEqual(['serper___web_search']);
  });
  it('returns [] when result has no tools', () => {
    expect(parseToolsResult({})).toEqual([]);
  });
});
