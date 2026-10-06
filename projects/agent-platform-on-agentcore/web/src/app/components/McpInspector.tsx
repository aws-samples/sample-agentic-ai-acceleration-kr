"use client";

import { useState, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiClient } from "@/lib/api-client";
import { cn, copyText } from "@/lib/utils";
import { EmptyState } from "@/app/components/PageHeader";
import {
  Loader2,
  CheckCircle2,
  XCircle,
  ChevronDown,
  ChevronUp,
  Play,
  Settings,
} from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { JsonTree } from "@/app/components/JsonTree";
// Schema→form→arguments logic is shared with the registry panel's tool try-out:
// both call the same servers, so a local copy would mean the two sending
// differently-typed arguments for one schema.
import {
  buildArgumentFields,
  buildArguments,
  countArguments,
  type ArgumentField,
} from "@/lib/mcp-tools";

/**
 * The MCP inspector: connect to an MCP server, list its tools, try one.
 *
 * Lived at `/mcp` as its own admin page; it is a Settings section now, because
 * "which MCP servers does this platform talk to, and do they answer" is
 * configuration an admin checks, not a place anyone works in daily. The route
 * still exists and redirects here, so an old bookmark lands on the same form.
 */
export function McpInspector() {
  const [endpoint, setEndpoint] = useState("");
  const [authorization, setAuthorization] = useState("");
  const [transportType, setTransportType] = useState<"http" | "stdio">("http");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [configCollapsed, setConfigCollapsed] = useState(false);

  const [isLoadingTools, setIsLoadingTools] = useState(false);
  const [toolsResult, setToolsResult] = useState<{
    total_servers: number;
    total_tools: number;
    servers: Array<{
      server_name: string;
      url: string;
      success: boolean;
      tools: Array<{
        name: string;
        description?: string;
        input_schema?: Record<string, any>;
      }>;
      error?: string;
    }>;
  } | null>(null);

  const [expandedServers, setExpandedServers] = useState<Set<string>>(new Set());
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  
  // Cognito login state
  const [cognitoClientId, setCognitoClientId] = useState("");
  const [cognitoUsername, setCognitoUsername] = useState("");
  const [cognitoPassword, setCognitoPassword] = useState("");
  const [cognitoClientSecret, setCognitoClientSecret] = useState("");
  const [cognitoUserPoolDomain, setCognitoUserPoolDomain] = useState("");
  // Empty, not "us-east-1": pre-filling the field made the browser override the
  // server's COGNITO_REGION on every login, so a pool in another region was
  // unreachable unless the operator noticed and retyped this box. The input
  // shows ap-northeast-1 as a placeholder instead.
  const [cognitoRegion, setCognitoRegion] = useState("");
  const [cognitoToken, setCognitoToken] = useState("");
  const [isLoadingCognito, setIsLoadingCognito] = useState(false);
  const [cognitoError, setCognitoError] = useState<string | null>(null);
  const [useClientSecret, setUseClientSecret] = useState(false);
  
  // Store tool states per tool (key: "serverName-toolName")
  const [toolStates, setToolStates] = useState<Map<string, {
    argumentFields: ArgumentField[];
    toolResult: {
      success: boolean;
      result?: any;
      error?: string;
    } | null;
    isExecuting: boolean;
  }>>(new Map());

  const apiClient = useMemo(() => {
    // Same-origin by default (Next.js rewrite proxy); override via NEXT_PUBLIC_API_URL.
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
    return new ApiClient({
      apiUrl,
    });
  }, []);

  const buildServerConfig = () => {
    const config: Record<string, any> = {
      type: transportType,
    };

    if (transportType === "http") {
      if (endpoint) {
        config.url = endpoint;
      }
      if (authorization) {
        config.apiKey = authorization;
      }
    } else if (transportType === "stdio") {
      if (command) {
        config.command = command;
      }
      if (args) {
        try {
          const parsedArgs = JSON.parse(args);
          if (Array.isArray(parsedArgs)) {
            config.args = parsedArgs;
          } else {
            config.args = [args];
          }
        } catch {
          config.args = args.split(/\s+/).filter((a) => a);
        }
      }
      if (env) {
        try {
          const parsedEnv = JSON.parse(env);
          if (typeof parsedEnv === "object") {
            config.env = parsedEnv;
          }
        } catch {
          // Ignore invalid JSON
        }
      }
    }

    return config;
  };

  const handleLoadTools = async () => {
    const serverConfig = buildServerConfig();

    if (transportType === "http" && !endpoint) {
      alert("Please enter an endpoint URL for HTTP transport");
      return;
    }

    if (transportType === "stdio" && !command) {
      alert("Please enter a command for stdio transport");
      return;
    }

    setIsLoadingTools(true);
    setToolsResult(null);
    setExpandedTools(new Set());
    setToolStates(new Map());

    try {
      const displayName = `server-${Date.now()}`;
      const mcpServers = {
        [displayName]: serverConfig,
      };

      const result = await apiClient.listMcpTools(mcpServers);
      setToolsResult(result);
      setExpandedServers(new Set(result.servers.map((s) => s.server_name)));
    } catch (error: any) {
      alert(`Failed to load MCP tools: ${error.message}`);
      setToolsResult(null);
    } finally {
      setIsLoadingTools(false);
    }
  };

  const toggleServerExpansion = (serverName: string) => {
    const newExpanded = new Set(expandedServers);
    if (newExpanded.has(serverName)) {
      newExpanded.delete(serverName);
    } else {
      newExpanded.add(serverName);
    }
    setExpandedServers(newExpanded);
  };

  const toggleToolExpansion = (toolKey: string) => {
    const newExpanded = new Set(expandedTools);
    if (newExpanded.has(toolKey)) {
      newExpanded.delete(toolKey);
    } else {
      newExpanded.add(toolKey);
    }
    setExpandedTools(newExpanded);
  };

  const getToolKey = (serverName: string, toolName: string) => {
    return `${serverName}-${toolName}`;
  };

  const initializeToolState = (
    serverName: string,
    tool: { name: string; description?: string; input_schema?: Record<string, any> }
  ) => {
    const toolKey = getToolKey(serverName, tool.name);

    // Check if state already exists
    if (toolStates.has(toolKey)) {
      return;
    }

    setToolStates(prev => {
      const newMap = new Map(prev);
      newMap.set(toolKey, {
        argumentFields: buildArgumentFields(tool.input_schema),
        toolResult: null,
        isExecuting: false,
      });
      return newMap;
    });
  };

  const updateArgumentField = (toolKey: string, index: number, value: any) => {
    setToolStates(prev => {
      const newMap = new Map(prev);
      const state = newMap.get(toolKey);
      if (state) {
        const newFields = [...state.argumentFields];
        newFields[index].value = value;
        newMap.set(toolKey, {
          ...state,
          argumentFields: newFields,
        });
      }
      return newMap;
    });
  };

  const handleExecuteTool = async (serverName: string, toolName: string) => {
    const toolKey = getToolKey(serverName, toolName);
    const state = toolStates.get(toolKey);
    if (!state) return;

    const serverConfig = buildServerConfig();
    const parsedArgs = buildArguments(state.argumentFields);

    // Set executing state
    setToolStates(prev => {
      const newMap = new Map(prev);
      const currentState = newMap.get(toolKey);
      if (currentState) {
        newMap.set(toolKey, {
          ...currentState,
          isExecuting: true,
          toolResult: null,
        });
      }
      return newMap;
    });

    try {
      const result = await apiClient.callMcpTool(
        serverName,
        serverConfig,
        toolName,
        parsedArgs
      );
      
      // Set result
      setToolStates(prev => {
        const newMap = new Map(prev);
        const currentState = newMap.get(toolKey);
        if (currentState) {
          newMap.set(toolKey, {
            ...currentState,
            isExecuting: false,
            toolResult: result,
          });
        }
        return newMap;
      });
    } catch (error: any) {
      setToolStates(prev => {
        const newMap = new Map(prev);
        const currentState = newMap.get(toolKey);
        if (currentState) {
          newMap.set(toolKey, {
            ...currentState,
            isExecuting: false,
            toolResult: {
              success: false,
              error: error.message || "Failed to execute tool",
            },
          });
        }
        return newMap;
      });
    }
  };

  const renderArgumentField = (toolKey: string, field: ArgumentField, index: number) => {
    const { key, schema, value } = field;
    const isRequired = schema.required === true;

    return (
      <div key={key} className="space-y-2 rounded-md border border-border bg-muted/40 p-2.5">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1">
            <Label htmlFor={`arg-${toolKey}-${index}`} className="font-mono text-xs font-semibold">
              {key}
              {isRequired && <span className="text-destructive ml-1">*</span>}
              {schema.type && (
                <span className="ml-1.5 font-sans text-xxs font-normal text-muted-foreground">
                  {schema.type}
                </span>
              )}
            </Label>
            {schema.description && (
              <p className="mt-1 text-xs leading-normal text-muted-foreground">
                {schema.description}
              </p>
            )}
          </div>
        </div>
        <div className="mt-2">
          {schema.type === "boolean" ? (
            <div className="flex items-center space-x-2">
              <Switch
                id={`arg-${toolKey}-${index}`}
                checked={Boolean(value)}
                onCheckedChange={(checked) => updateArgumentField(toolKey, index, checked)}
              />
              <span className="text-sm text-muted-foreground">
                {value ? "True" : "False"}
              </span>
            </div>
          ) : schema.type === "array" || schema.type === "object" ? (
            <Textarea
              id={`arg-${toolKey}-${index}`}
              value={typeof value === "string" ? value : JSON.stringify(value, null, 2)}
              onChange={(e) => updateArgumentField(toolKey, index, e.target.value)}
              className="font-mono text-sm"
              rows={4}
              placeholder={
                schema.type === "array"
                  ? '["item1", "item2"]'
                  : '{"key": "value"}'
              }
            />
          ) : schema.type === "number" || schema.type === "integer" ? (
            <Input
              id={`arg-${toolKey}-${index}`}
              type="number"
              value={value === undefined || value === "" ? "" : String(value)}
              onChange={(e) => updateArgumentField(toolKey, index, e.target.value)}
              placeholder={schema.default !== undefined ? String(schema.default) : ""}
            />
          ) : (
            <Input
              id={`arg-${toolKey}-${index}`}
              type="text"
              value={value === undefined ? "" : String(value)}
              onChange={(e) => updateArgumentField(toolKey, index, e.target.value)}
              placeholder={schema.default !== undefined ? String(schema.default) : ""}
            />
          )}
          {schema.enum && (
            <p className="mt-1 text-xs leading-normal text-muted-foreground">
              Allowed values: {schema.enum.join(", ")}
            </p>
          )}
          {schema.default !== undefined && (
            <p className="mt-1 text-xs leading-normal text-muted-foreground">
              Default: {JSON.stringify(schema.default)}
            </p>
          )}
        </div>
      </div>
    );
  };

  return (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          {/* Left: Server Configuration */}
          <div className={`lg:col-span-1 ${configCollapsed ? "hidden lg:block" : ""}`}>
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Server Configuration</CardTitle>
                    <CardDescription>
                      Configure your MCP server connection
                    </CardDescription>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfigCollapsed(!configCollapsed)}
                    className="lg:hidden"
                  >
                    {configCollapsed ? <ChevronDown /> : <ChevronUp />}
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid gap-2">
                  <Label htmlFor="transportType">Transport Type</Label>
                  <Select
                    value={transportType}
                    onValueChange={(value) => setTransportType(value as "http" | "stdio")}
                  >
                    <SelectTrigger id="transportType">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="http">HTTP/Streamable HTTP</SelectItem>
                      <SelectItem value="stdio">stdio</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {transportType === "http" ? (
                  <>
                    <div className="grid gap-2">
                      <Label htmlFor="endpoint">
                        Endpoint URL <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="endpoint"
                        placeholder="http://localhost:8000/mcp"
                        value={endpoint}
                        onChange={(e) => setEndpoint(e.target.value)}
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="authorization">
                        Authorization (API Key / Bearer Token)
                      </Label>
                      <Input
                        id="authorization"
                        type="password"
                        placeholder="Bearer token or API key"
                        value={authorization}
                        onChange={(e) => setAuthorization(e.target.value)}
                      />
                    </div>
                  </>
                ) : (
                  <>
                    <div className="grid gap-2">
                      <Label htmlFor="command">
                        Command <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="command"
                        placeholder="uvx"
                        value={command}
                        onChange={(e) => setCommand(e.target.value)}
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="args">Arguments (JSON array)</Label>
                      <Input
                        id="args"
                        placeholder='["package@version"]'
                        value={args}
                        onChange={(e) => setArgs(e.target.value)}
                      />
                      <p className="text-xs text-muted-foreground">
                        Enter as JSON array or space-separated values
                      </p>
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="env">Environment Variables (JSON)</Label>
                      <Textarea
                        id="env"
                        placeholder='{"KEY": "value"}'
                        value={env}
                        onChange={(e) => setEnv(e.target.value)}
                        className="font-mono text-sm"
                        rows={3}
                      />
                    </div>
                  </>
                )}

                <Button
                  onClick={handleLoadTools}
                  disabled={isLoadingTools}
                  className="w-full"
                >
                  {isLoadingTools ? (
                    <>
                      <Loader2 className="size-3.5 animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    <>
                      <Settings className="size-3.5" />
                      Connect & Load Tools
                    </>
                  )}
                </Button>
              </CardContent>
            </Card>

            {/* Cognito Authentication Card */}
            <Card className="mt-3">
              <CardHeader>
                <CardTitle>Cognito Authentication</CardTitle>
                <CardDescription>
                  Get JWT token from Cognito for MCP server authorization
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid gap-2">
                  <Label htmlFor="cognitoClientId">
                    Client ID <span className="text-destructive">*</span>
                  </Label>
                  <Input
                    id="cognitoClientId"
                    placeholder="Cognito User Pool Client ID"
                    value={cognitoClientId}
                    onChange={(e) => setCognitoClientId(e.target.value)}
                  />
                </div>
                
                <div className="flex items-center space-x-2">
                  <Switch
                    id="useClientSecret"
                    checked={useClientSecret}
                    onCheckedChange={setUseClientSecret}
                  />
                  <Label htmlFor="useClientSecret" className="text-sm">
                    Use Client Secret (Machine-to-Machine)
                  </Label>
                </div>

                {!useClientSecret ? (
                  <>
                    <div className="grid gap-2">
                      <Label htmlFor="cognitoUsername">
                        Username <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="cognitoUsername"
                        placeholder="Cognito username"
                        value={cognitoUsername}
                        onChange={(e) => setCognitoUsername(e.target.value)}
                      />
                    </div>
                    
                    <div className="grid gap-2">
                      <Label htmlFor="cognitoPassword">
                        Password <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="cognitoPassword"
                        type="password"
                        placeholder="Cognito password"
                        value={cognitoPassword}
                        onChange={(e) => setCognitoPassword(e.target.value)}
                      />
                    </div>
                    
                    <div className="grid gap-2">
                      <Label htmlFor="cognitoClientSecret">
                        Client Secret <span className="text-muted-foreground">(Optional)</span>
                      </Label>
                      <Input
                        id="cognitoClientSecret"
                        type="password"
                        placeholder="Client secret (if required)"
                        value={cognitoClientSecret}
                        onChange={(e) => setCognitoClientSecret(e.target.value)}
                      />
                      <p className="text-xs text-muted-foreground">
                        Some Cognito clients require a client secret for authentication
                      </p>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="grid gap-2">
                      <Label htmlFor="cognitoClientSecretM2M">
                        Client Secret <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="cognitoClientSecretM2M"
                        type="password"
                        placeholder="Client secret"
                        value={cognitoClientSecret}
                        onChange={(e) => setCognitoClientSecret(e.target.value)}
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="cognitoUserPoolDomain">
                        User Pool Domain <span className="text-destructive">*</span>
                      </Label>
                      <Input
                        id="cognitoUserPoolDomain"
                        placeholder="your-domain.auth.ap-northeast-1.amazoncognito.com"
                        value={cognitoUserPoolDomain}
                        onChange={(e) => setCognitoUserPoolDomain(e.target.value)}
                      />
                      <p className="text-xs text-muted-foreground">
                        Format: your-domain.auth.{cognitoRegion || "ap-northeast-1"}.amazoncognito.com
                      </p>
                    </div>
                  </>
                )}
                
                <div className="grid gap-2">
                  <Label htmlFor="cognitoRegion">
                    Region <span className="text-muted-foreground">(Optional)</span>
                  </Label>
                  <Input
                    id="cognitoRegion"
                    placeholder="ap-northeast-1"
                    value={cognitoRegion}
                    onChange={(e) => setCognitoRegion(e.target.value)}
                  />
                </div>
                
                <Button
                  type="button"
                  variant="outline"
                  onClick={async () => {
                    if (!cognitoClientId) {
                      setCognitoError("Client ID is required");
                      return;
                    }
                    
                    if (!useClientSecret) {
                      if (!cognitoUsername || !cognitoPassword) {
                        setCognitoError("Username and password are required");
                        return;
                      }
                    } else {
                      if (!cognitoClientSecret) {
                        setCognitoError("Client secret is required for M2M authentication");
                        return;
                      }
                      if (!cognitoUserPoolDomain) {
                        setCognitoError("User Pool Domain is required for M2M authentication");
                        return;
                      }
                    }
                    
                    setIsLoadingCognito(true);
                    setCognitoError(null);
                    setCognitoToken("");
                    
                    try {
                      const result = await apiClient.cognitoLogin({
                        client_id: cognitoClientId,
                        username: useClientSecret ? undefined : cognitoUsername,
                        password: useClientSecret ? undefined : cognitoPassword,
                        client_secret: cognitoClientSecret || undefined,
                        user_pool_domain: useClientSecret ? cognitoUserPoolDomain : undefined,
                        region: cognitoRegion,
                        auth_flow: useClientSecret ? "CLIENT_CREDENTIALS" : "USER_PASSWORD_AUTH",
                      });
                      
                      if (result.success && result.access_token) {
                        setCognitoToken(result.access_token);
                        setCognitoError(null);
                        // Automatically fill in the authorization field
                        setAuthorization(result.access_token);
                      } else {
                        setCognitoError(result.error || "Login failed");
                        setCognitoToken("");
                      }
                    } catch (error: any) {
                      setCognitoError(error.message || "Failed to login");
                      setCognitoToken("");
                    } finally {
                      setIsLoadingCognito(false);
                    }
                  }}
                  disabled={isLoadingCognito}
                  className="w-full"
                >
                  {isLoadingCognito ? (
                    <>
                      <Loader2 className="size-3.5 animate-spin" />
                      Logging in...
                    </>
                  ) : (
                    "Get Token"
                  )}
                </Button>
                
                {cognitoError && (
                  <div className="rounded-md border border-destructive/30 bg-destructive/[0.07] p-2 text-xs text-destructive">
                    {cognitoError}
                  </div>
                )}
                
                {cognitoToken && (
                  <div className="grid gap-2">
                    <Label className="caps-label-xs text-muted-foreground">Access Token</Label>
                    <div className="rounded-md border border-border bg-muted/40 p-2.5">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs text-success">토큰을 받았습니다</span>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            void copyText(cognitoToken);
                          }}
                          className="h-6 text-xs"
                        >
                          Copy
                        </Button>
                      </div>
                      <Input
                        type="password"
                        value={cognitoToken}
                        readOnly
                        className="font-mono text-xs"
                      />
                      <p className="mt-2 text-xs leading-normal text-muted-foreground">
                        Token has been automatically filled in the Authorization field above
                      </p>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          {/* Right: Available Tools with Expandable Test */}
          <div className="lg:col-span-2">
            {!toolsResult ? (
              <Card>
                <EmptyState
                  icon={Settings}
                  title="연결된 MCP 서버가 없습니다"
                  description="왼쪽에서 서버를 설정하고 연결하면 사용할 수 있는 도구가 여기에 나타납니다."
                />
              </Card>
            ) : (
              <Card>
                <CardHeader>
                  <CardTitle>Available Tools</CardTitle>
                  <CardDescription>
                    {toolsResult.total_tools} tools from {toolsResult.total_servers} server
                    {toolsResult.total_servers !== 1 ? "s" : ""}
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    {toolsResult.servers.map((server) => (
                      <div
                        key={server.server_name}
                        className="rounded-md border border-border bg-muted/40 p-2.5"
                      >
                        <button
                          type="button"
                          onClick={() => toggleServerExpansion(server.server_name)}
                          className="w-full flex items-center justify-between text-left"
                        >
                          <div className="flex items-center gap-2">
                            {server.success ? (
                              <CheckCircle2 className="size-3.5 shrink-0 text-success" />
                            ) : (
                              <XCircle className="size-3.5 shrink-0 text-destructive" />
                            )}
                            <span className="text-xs font-semibold">{server.server_name}</span>
                            <span className="truncate font-mono text-xxs text-muted-foreground">
                              {server.url}
                            </span>
                          </div>
                          {expandedServers.has(server.server_name) ? (
                            <ChevronUp className="h-4 w-4" />
                          ) : (
                            <ChevronDown className="h-4 w-4" />
                          )}
                        </button>

                        {expandedServers.has(server.server_name) && (
                          <div className="mt-2 pl-6 space-y-2">
                            {server.success ? (
                              <>
                                {server.tools.length > 0 ? (
                                  <div className="space-y-2">
                                    {server.tools.map((tool, idx) => {
                                      const isExpanded = expandedTools.has(`${server.server_name}-${tool.name}`);
                                      const toolKey = `${server.server_name}-${tool.name}`;
                                      
                                      const argCount = countArguments(tool.input_schema);

                                      return (
                                        <div
                                          key={idx}
                                          className="rounded-md border border-border bg-card"
                                        >
                                          <button
                                            type="button"
                                            onClick={() => {
                                              if (!isExpanded) {
                                                initializeToolState(server.server_name, tool);
                                              }
                                              toggleToolExpansion(toolKey);
                                            }}
                                            className="w-full rounded-md p-2.5 text-left transition-colors hover:bg-accent"
                                          >
                                            <div className="flex items-start justify-between gap-2">
                                              <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2 font-mono text-xs font-semibold">
                                                  {tool.name}
                                                  {argCount > 0 && (
                                                    <span className="font-sans text-xxs font-normal text-muted-foreground">
                                                      {argCount} {argCount === 1 ? 'arg' : 'args'}
                                                    </span>
                                                  )}
                                                </div>
                                                {tool.description && (
                                                  <div className="mt-0.5 text-xs leading-normal text-muted-foreground">
                                                    {tool.description}
                                                  </div>
                                                )}
                                              </div>
                                              {isExpanded ? (
                                                <ChevronUp className="h-4 w-4 flex-shrink-0" />
                                              ) : (
                                                <ChevronDown className="h-4 w-4 flex-shrink-0" />
                                              )}
                                            </div>
                                          </button>

                                          {isExpanded && (() => {
                                            const state = toolStates.get(toolKey);
                                            if (!state) {
                                              // Initialize if not exists
                                              initializeToolState(server.server_name, tool);
                                              return null;
                                            }
                                            
                                            return (
                                              <div className="border-t p-4 space-y-4">
                                                {/* Show input schema for debugging */}
                                                {tool.input_schema && (
                                                  <div className="space-y-2 border rounded-md p-3 bg-muted/50">
                                                    <div className="flex items-center justify-between">
                                                      <Label className="caps-label-xs text-muted-foreground">Input Schema</Label>
                                                      <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        onClick={() => {
                                                          const schemaStr = JSON.stringify(tool.input_schema, null, 2);
                                                          void copyText(schemaStr);
                                                        }}
                                                      >
                                                        Copy
                                                      </Button>
                                                    </div>
                                                    <div className="max-h-96 overflow-auto rounded border border-border bg-card p-2">
                                                      <JsonTree
                                                        value={tool.input_schema}
                                                        collapsed={2}
                                                      />
                                                    </div>
                                                  </div>
                                                )}

                                                {state.argumentFields.length > 0 ? (
                                                  <div className="space-y-4">
                                                    <Label className="caps-label-xs text-muted-foreground">
                                                      Arguments ({state.argumentFields.length})
                                                    </Label>
                                                    {state.argumentFields.map((field, fieldIdx) =>
                                                      renderArgumentField(toolKey, field, fieldIdx)
                                                    )}
                                                  </div>
                                                ) : (
                                                  <div className="space-y-2">
                                                    <Label className="caps-label-xs text-muted-foreground">Arguments</Label>
                                                    <div className="rounded-md border border-border bg-muted/40 p-2.5 text-xs leading-normal text-muted-foreground">
                                                      {tool.input_schema 
                                                        ? "This tool's input schema doesn't define properties. You can still call it with empty arguments or check the schema above."
                                                        : "This tool doesn't have an input schema defined. You can try calling it with empty arguments."}
                                                    </div>
                                                  </div>
                                                )}

                                                <Button
                                                  onClick={() => handleExecuteTool(server.server_name, tool.name)}
                                                  disabled={state.isExecuting}
                                                  className="w-full"
                                                >
                                                  {state.isExecuting ? (
                                                    <>
                                                      <Loader2 className="size-3.5 animate-spin" />
                                                      Executing...
                                                    </>
                                                  ) : (
                                                    <>
                                                      <Play className="size-3.5" />
                                                      Execute Tool
                                                    </>
                                                  )}
                                                </Button>

                                                {state.toolResult && (
                                                  <div className="space-y-2">
                                                    <Label>Result</Label>
                                                    <div
                                                      className={cn(
                                                        "rounded-md border p-2.5",
                                                        state.toolResult.success
                                                          ? "border-success/25 bg-success/[0.07]"
                                                          : "border-destructive/25 bg-destructive/[0.07]"
                                                      )}
                                                    >
                                                      {state.toolResult.success ? (
                                                        <div className="overflow-auto max-h-96">
                                                          {/* The whole CallToolResult, not the unwrapped payload the
                                                              registry panel shows — this page exists to inspect the
                                                              envelope, so content blocks and _meta stay visible. */}
                                                          <JsonTree
                                                            value={state.toolResult.result}
                                                            collapsed={2}
                                                          />
                                                        </div>
                                                      ) : (
                                                        <div className="text-xs leading-normal text-destructive">
                                                          Error: {state.toolResult.error}
                                                        </div>
                                                      )}
                                                    </div>
                                                  </div>
                                                )}
                                              </div>
                                            );
                                          })()}
                                        </div>
                                      );
                                    })}
                                  </div>
                                ) : (
                                  <p className="text-xs text-muted-foreground">
                                    No tools available from this server
                                  </p>
                                )}
                              </>
                            ) : (
                              <p className="text-xs text-destructive">
                                Error: {server.error || "Unknown error"}
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}
          </div>
        </div>
  );
}
