import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ComponentDeepDive,
  InterruptedChoice,
  RelationshipInspector,
  RepositoryForm,
  RepositoryOverview,
  RepositoryWorkspace,
  SystemMapExplorer,
  WorkspaceChat,
  layoutSystemMap,
  type ChatState,
} from "./page";

type ChatWorkspace = Parameters<typeof WorkspaceChat>[0]["workspace"];
type OverviewWorkspace = Parameters<typeof RepositoryOverview>[0]["workspace"];
type OverviewMap = Parameters<typeof RepositoryOverview>[0]["map"];

const workspace = {
  workspace_id: "workspace-test",
  source_available: true,
  source_status: "available",
} as ChatWorkspace;

const overviewMap = {
  title: "Order processing",
  summary: "Receives orders and coordinates fulfillment.",
  primary_flow_id: "flow-order",
  boundaries: [
    { id: "interface", label: "Interface", kind: "interface" },
    { id: "core", label: "Core", kind: "application" },
  ],
  nodes: [
    {
      id: "endpoint",
      component_id: "orders",
      label: "Order endpoint",
      kind: "entrypoint",
      boundary: "interface",
      summary: "Accepts an order.",
      description: "Validates the incoming HTTP order contract.",
      responsibilities: ["Accept order"],
      entry_points: [],
      exit_points: [],
      inputs: ["Order request"],
      outputs: ["Validated order"],
      http_contracts: [],
      confidence: "verified",
      evidence: ["src/OrderEndpoint.java:12"],
    },
    {
      id: "service",
      component_id: "orders",
      label: "Order service",
      kind: "service",
      boundary: "core",
      summary: "Coordinates fulfillment.",
      description: "Coordinates the business operation and produces a result.",
      responsibilities: ["Coordinate fulfillment"],
      entry_points: [],
      exit_points: [],
      inputs: ["Validated order"],
      outputs: ["Order result"],
      http_contracts: [],
      confidence: "verified",
      evidence: ["src/OrderService.java:20"],
    },
  ],
  edges: [
    {
      id: "accept-order",
      source: "endpoint",
      target: "service",
      label: "submit",
      kind: "call",
      protocol: "Java method call",
      data: "Validated order",
      action: "Submits the validated order for fulfillment.",
      confidence: "verified",
      flow_ids: ["flow-order"],
      evidence: ["src/OrderEndpoint.java:30"],
    },
  ],
  flows: [
    {
      id: "flow-order",
      name: "Submit order",
      description: "An HTTP order is validated and sent to fulfillment.",
      trigger: "POST /orders",
      input: "Order request",
      outcome: "Order result",
      confidence: "verified",
      node_ids: ["endpoint", "service"],
      steps: [
        {
          order: 1,
          node_id: "service",
          edge_id: "accept-order",
          from: "Order endpoint",
          to: "Order service",
          data: "Validated order",
          action: "Calls the order service.",
          result: "Fulfillment begins.",
          evidence: ["src/OrderEndpoint.java:30"],
        },
      ],
    },
  ],
} as OverviewMap;

const overviewWorkspace = {
  schema_version: "2.1",
  workspace_id: "overview-workspace",
  repository: {
    location: "https://example.test/order-processing.git",
    revision: "abc123",
    source_cached: true,
  },
  source_available: true,
  settings: { model: "gpt-5.6-sol", reasoning: "high" },
  status: "completed",
  phase: "completed",
  message: "Ready",
  progress: 100,
  orientation: {
    purpose: "Processes customer orders from intake through fulfillment.",
    architecture_style: "Layered Java application",
    technologies: [
      {
        name: "Java 21",
        category: "language",
        confidence: "verified",
        evidence: ["pom.xml:14"],
      },
      {
        name: "Maven",
        category: "build",
        confidence: "verified",
        evidence: ["pom.xml:1"],
      },
    ],
  },
  system_map: overviewMap,
  components: [
    {
      id: "orders",
      display_name: "Orders",
      kind: "service",
      purpose: "Processes orders",
      confidence: "verified",
    },
  ],
  updated_at: "2026-07-29T00:00:00Z",
} as OverviewWorkspace;

function ChatHarness({
  available = true,
  initialMessages = [],
}: {
  available?: boolean;
  initialMessages?: ChatState["messages"];
}) {
  const [state, setState] = useState<ChatState>({
    messages: initialMessages,
    question: "",
    status: "",
    error: "",
    sessionId: null,
    failedQuestion: null,
    warning: null,
    scopeExpanded: false,
  });
  const requestRef = useRef<AbortController | null>(null);
  const restoreRef = useRef<AbortController | null>(null);
  return (
    <WorkspaceChat
      workspace={{ ...workspace, source_available: available }}
      state={state}
      onStateChange={setState}
      requestRef={requestRef}
      restoreRef={restoreRef}
    />
  );
}

function stream(events: Record<string, unknown>[]) {
  return new Response(
    events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("WorkspaceChat", () => {
  it("mounts without calling scrollIntoView", () => {
    const scrollIntoView = vi.fn(() => {
      throw new Error("unsupported");
    });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    render(<ChatHarness />);

    expect(screen.getByText("Repository assistant")).toBeTruthy();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("renders a completed streamed answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        stream([
          {
            type: "status",
            message: "Searching evidence",
            session_id: "session-one",
          },
          {
            type: "complete",
            answer: "Execution begins in `Main.main`.",
            session_id: "session-one",
            confidence: "verified",
            retrieval_mode: "lightweight_lexical",
            answer_mode: "ollama",
            provider: "ollama",
            model: "qwen2.5-coder:7b",
            auth_mode: "local",
            generation_warning: "The answer used the strongest available repository evidence.",
            sources: [
              {
                file: "src/Main.java",
                symbol: "Main.main",
                start_line: 8,
                end_line: 12,
              },
            ],
          },
        ]),
      ),
    );
    const user = userEvent.setup();
    render(<ChatHarness />);

    await user.type(
      screen.getByPlaceholderText("Ask about this system…"),
      "Where does execution begin?",
    );
    await user.click(screen.getByRole("button", { name: /Send/ }));

    expect(
      await screen.findByText(/Execution begins in/),
    ).toBeTruthy();
    expect(screen.getByText("Local Ollama")).toBeTruthy();
    expect(screen.getByText("1 source references")).toBeTruthy();
    expect(
      screen.getByText("The answer used the strongest available repository evidence."),
    ).toBeTruthy();
    expect(screen.queryByText(/qwen2\.5-coder/)).toBeNull();
    expect(screen.queryByText(/verified confidence/i)).toBeNull();
    expect(screen.queryByText(/lightweight lexical/i)).toBeNull();
    expect(screen.queryByText(/config profile/i)).toBeNull();
  });

  it("contains long answers inside the keyboard-scrollable conversation log", () => {
    const longIdentifier = `VeryLongRepositoryIdentifier${"WithoutBreaks".repeat(20)}`;
    const { container } = render(
      <ChatHarness
        initialMessages={[
          {
            role: "assistant",
            content: `The relevant symbol is \`${longIdentifier}\`.`,
          },
        ]}
      />,
    );

    const conversation = screen.getByRole("log", { name: "Chat conversation" });
    const answer = container.querySelector(".chat-message.assistant");

    expect(conversation.getAttribute("tabindex")).toBe("0");
    expect(answer).toBeTruthy();
    expect(answer?.textContent).toContain(longIdentifier);
  });

  it("retries a failed turn without duplicating the user message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        stream([
          {
            type: "error",
            error: "Temporary Ollama failure",
            code: "ollama_unavailable",
            retryable: true,
          },
        ]),
      )
      .mockResolvedValueOnce(
        stream([
          {
            type: "complete",
            answer: "Recovered answer",
            session_id: "session-retry",
          },
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ChatHarness />);

    await user.type(
      screen.getByPlaceholderText("Ask about this system…"),
      "Trace the request",
    );
    await user.click(screen.getByRole("button", { name: /Send/ }));
    await user.click(await screen.findByRole("button", { name: "Retry question" }));

    expect(await screen.findByText("Recovered answer")).toBeTruthy();
    expect(screen.getAllByText("Trace the request")).toHaveLength(1);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("offers source restoration instead of enabling chat without source", () => {
    render(<ChatHarness available={false} />);

    expect(screen.getByText("Repository source is not cached")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Restore source" })).toBeTruthy();
    expect(
      (screen.getByPlaceholderText("Ask about this system…") as HTMLTextAreaElement)
        .disabled,
    ).toBe(true);
  });
});

describe("InterruptedChoice", () => {
  it("resumes cached synthesis without restarting repository discovery", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "running" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onUpdated = vi.fn();
    const user = userEvent.setup();
    render(
      <InterruptedChoice
        workspace={{
          workspace_id: "workspace-test",
          repository: {
            location: "https://example.test/team/project.git",
            revision: "revision",
            source_cached: true,
          },
          settings: { model: "gpt-5.6-sol", reasoning: "high" },
          schema_version: "2.1",
          status: "interrupted",
          phase: "interrupted",
          message: "Interrupted",
          progress: 55,
          components: [],
          static_fallback_available: true,
          recovery: {
            state: "action_required",
            action: "retry_map",
            reason: "Cached evidence is ready.",
            attempts: 0,
            interrupted_at: "2026-07-29T00:00:00Z",
          },
          updated_at: "2026-07-29T00:00:00Z",
        }}
        onUpdated={onUpdated}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Resume map synthesis" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/v2/workspaces/workspace-test/map/retry",
        { method: "POST" },
      ),
    );
    expect(onUpdated).toHaveBeenCalledWith(null);
  });
});

describe("developer overview", () => {
  it("shows purpose, architecture, useful metrics, technologies, and journeys", async () => {
    const user = userEvent.setup();
    render(
      <RepositoryOverview
        workspace={overviewWorkspace}
        map={overviewMap}
        onOpenMap={vi.fn()}
      />,
    );

    expect(screen.getByText("Processes customer orders from intake through fulfillment.")).toBeTruthy();
    expect(screen.getByText("Layered Java application")).toBeTruthy();
    expect(screen.getByText("Java 21")).toBeTruthy();
    expect(screen.getByText("Maven")).toBeTruthy();
    expect(screen.getByText("Submit order")).toBeTruthy();
    expect(screen.getByText("Source proof for this handoff")).toBeTruthy();
    expect(screen.getByText("Top-level components")).toBeTruthy();
    expect(screen.getByText("End-to-end flows")).toBeTruthy();
    expect(screen.queryByText("Packages")).toBeNull();
    expect(screen.queryByText("Files")).toBeNull();
    expect(screen.queryByText("Endpoints")).toBeNull();

    await user.click(screen.getByText("Source proof for this handoff"));
    expect(screen.getByText("src/OrderEndpoint.java")).toBeTruthy();
    expect(screen.getByText("30")).toBeTruthy();
  });

  it("opens a component directly in the interactive diagram view", async () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    render(
      <ComponentDeepDive
        workspace={overviewWorkspace}
        component={overviewWorkspace.components[0]}
        data={{
          summary: overviewWorkspace.components[0],
          status: { status: "completed" },
          artifact: {
            component_id: "orders",
            summary: "Processes an accepted order.",
            responsibilities: ["Coordinate fulfillment"],
            entrypoints: [],
            exit_points: [],
            source: "static_analysis",
            generation: { provider: "static", model: null, reasoning: null },
            diagram: overviewMap,
          },
        }}
        loading={false}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText("Processes an accepted order.")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Full screen" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Overview" })).toBeNull();
  });
});

describe("repository identity", () => {
  it("does not expose the Git revision in the active workspace header", () => {
    render(
      <RepositoryWorkspace
        workspace={overviewWorkspace}
        onClose={vi.fn()}
        onOpenDeepDive={vi.fn()}
      />,
    );

    expect(screen.getByText("order-processing")).toBeTruthy();
    expect(screen.queryByText("abc123")).toBeNull();
  });

  it("shows recent workspace status without the Git revision", () => {
    render(
      <RepositoryForm
        recent={[overviewWorkspace]}
        onCreated={vi.fn()}
      />,
    );

    expect(screen.getByText("completed")).toBeTruthy();
    expect(screen.queryByText(/abc123/)).toBeNull();
  });
});

describe("system map clarity", () => {
  it("omits the minimap and opens chat as a separate map panel", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const extraNodes = Array.from({ length: 9 }, (_, index) => ({
      ...overviewMap.nodes[1],
      id: `extra-${index}`,
      label: `Additional component ${index + 1}`,
    }));
    const largeMap = {
      ...overviewMap,
      nodes: [...overviewMap.nodes, ...extraNodes],
    } as OverviewMap;
    const user = userEvent.setup();
    const { container } = render(
      <SystemMapExplorer map={largeMap} workspace={overviewWorkspace} />,
    );

    await user.click(await screen.findByRole("button", { name: "Ask ArchAI" }));

    expect(container.querySelector(".react-flow__minimap")).toBeNull();
    expect(container.querySelector(".map-stage.drawer-open")).toBeTruthy();
    expect(container.querySelector(".react-flow-frame")).toBeTruthy();
    expect(container.querySelector("aside.map-drawer")).toBeTruthy();
    expect(screen.getByText("Repository assistant")).toBeTruthy();
  });

  it("uses ELK route points and strongly dims unrelated edges in flow focus", async () => {
    const resultNode = {
      ...overviewMap.nodes[1],
      id: "result",
      label: "Order result",
      summary: "Returns the fulfillment result.",
      description: "Maps the fulfillment result for the caller.",
    };
    const map = {
      ...overviewMap,
      nodes: [...overviewMap.nodes, resultNode],
      edges: [
        ...overviewMap.edges,
        {
          ...overviewMap.edges[0],
          id: "unrelated",
          source: "service",
          target: "endpoint",
          flow_ids: [],
        },
        {
          ...overviewMap.edges[0],
          id: "inside-core",
          source: "service",
          target: "result",
          flow_ids: [],
        },
      ],
    } as OverviewMap;

    const layout = await layoutSystemMap(
      map,
      map.flows[0],
      null,
      null,
      "flow",
      "RIGHT",
    );
    const active = layout.edges.find((edge) => edge.id === "accept-order");
    const unrelated = layout.edges.find((edge) => edge.id === "unrelated");
    const inside = layout.edges.find((edge) => edge.id === "inside-core");
    const boundary = layout.nodes.find((node) => node.id === "boundary:core")!;
    const source = layout.nodes.find((node) => node.id === "service")!;
    const target = layout.nodes.find((node) => node.id === "result")!;
    const insidePoints = (
      inside?.data as { routePoints: { x: number; y: number }[] }
    ).routePoints;

    expect((active?.data as { routePoints: unknown[] }).routePoints.length).toBeGreaterThanOrEqual(2);
    expect((active?.data as { step: number }).step).toBe(1);
    expect(unrelated?.style?.opacity).toBe(0.16);
    expect(insidePoints[0]).toEqual({
      x: boundary.position.x + source.position.x + 272,
      y: boundary.position.y + source.position.y + 72,
    });
    expect(insidePoints.at(-1)).toEqual({
      x: boundary.position.x + target.position.x,
      y: boundary.position.y + target.position.y + 72,
    });

    const verticalLayout = await layoutSystemMap(
      map,
      map.flows[0],
      null,
      null,
      "flow",
      "DOWN",
    );
    const verticalBoundary = verticalLayout.nodes.find(
      (node) => node.id === "boundary:core",
    )!;
    const verticalSource = verticalLayout.nodes.find((node) => node.id === "service")!;
    const verticalTarget = verticalLayout.nodes.find((node) => node.id === "result")!;
    const verticalPoints = (
      verticalLayout.edges.find((edge) => edge.id === "inside-core")?.data as {
        routePoints: { x: number; y: number }[];
      }
    ).routePoints;
    expect(verticalPoints[0]).toEqual({
      x: verticalBoundary.position.x + verticalSource.position.x + 136,
      y: verticalBoundary.position.y + verticalSource.position.y + 144,
    });
    expect(verticalPoints.at(-1)).toEqual({
      x: verticalBoundary.position.x + verticalTarget.position.x + 136,
      y: verticalBoundary.position.y + verticalTarget.position.y,
    });
  });

  it("explains a selected relationship without placing its long label on the graph", () => {
    render(
      <RelationshipInspector
        edge={overviewMap.edges[0]}
        source={overviewMap.nodes[0]}
        target={overviewMap.nodes[1]}
        step={overviewMap.flows[0].steps[0]}
      />,
    );

    expect(screen.getByText("Order endpoint → Order service")).toBeTruthy();
    expect(screen.getByText("Validated order")).toBeTruthy();
    expect(screen.getByText("Java method call")).toBeTruthy();
    expect(screen.getByText("Fulfillment begins.")).toBeTruthy();
  });

  it("opens the complete map in a dialog and closes it with Escape", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const user = userEvent.setup();
    render(<SystemMapExplorer map={overviewMap} workspace={overviewWorkspace} />);

    await user.click(await screen.findByRole("button", { name: "Full screen" }));
    expect(screen.getByRole("dialog", { name: "Order processing full screen map" })).toBeTruthy();

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Order processing full screen map" })).toBeNull(),
    );
  });
});
