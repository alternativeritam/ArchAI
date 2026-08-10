"use client";

import {
  Background,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import ELK from "elkjs/lib/elk.bundled.js";
import type { ElkExtendedEdge, ElkNode, ElkPoint } from "elkjs/lib/elk-api";
import {
  Component,
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API_BASE = process.env.NEXT_PUBLIC_ARCHAI_API_BASE_URL ?? "http://127.0.0.1:8000";
const elk = new ELK();

type Confidence = "verified" | "inferred" | "not_determined";
type SourceField = { name: string; type: string; required: boolean; line: number };
type HttpParameter = {
  name: string;
  java_name: string;
  type: string;
  source: string;
  required: boolean;
  fields: SourceField[];
  line: number;
};
type HttpContract = {
  framework: string;
  methods: string[];
  path: string;
  handler: string;
  request: {
    parameters: HttpParameter[];
    body: { type: string; fields: SourceField[] } | null;
  };
  response: {
    type: string;
    payload_type: string;
    fields: SourceField[];
    status_codes: { code: number; reason: string; evidence: string }[];
  };
  evidence: string[];
};
type EntryPoint = {
  id?: string;
  label: string;
  kind: string;
  file: string;
  line: number;
  confidence: Confidence;
  http_contract?: HttpContract;
};
type ExitPoint = { label: string; kind: string; description: string };
type MapNode = {
  id: string;
  component_id: string | null;
  label: string;
  kind:
    | "entrypoint"
    | "service"
    | "process"
    | "decision"
    | "data_store"
    | "configuration"
    | "external"
    | "outcome";
  boundary: string;
  summary: string;
  description: string;
  responsibilities: string[];
  entry_points: EntryPoint[];
  exit_points: ExitPoint[];
  inputs: string[];
  outputs: string[];
  http_contracts: HttpContract[];
  confidence: Confidence;
  evidence: string[];
};
type MapEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: string;
  protocol: string;
  data: string;
  action: string;
  confidence: Confidence;
  flow_ids: string[];
  evidence: string[];
};
type FlowStep = {
  order: number;
  node_id: string;
  edge_id: string;
  from: string;
  to: string;
  data: string;
  action: string;
  result: string;
  evidence: string[];
};
type MapFlow = {
  id: string;
  name: string;
  description: string;
  trigger: string;
  input: string;
  outcome: string;
  confidence: Confidence;
  node_ids: string[];
  steps: FlowStep[];
};
type SystemMap = {
  schema_version?: string;
  title: string;
  summary: string;
  primary_flow_id: string | null;
  boundaries: { id: string; label: string; kind: string }[];
  nodes: MapNode[];
  edges: MapEdge[];
  flows: MapFlow[];
  source?: string;
  generation?: {
    provider: string;
    model: string | null;
    reasoning: string | null;
  };
};
type ComponentSummary = {
  id: string;
  display_name: string;
  kind: string;
  purpose: string;
  confidence: Confidence;
};
type Technology = {
  name: string;
  category: "language" | "build" | "framework";
  confidence: Confidence;
  evidence: string[];
};
type WorkspaceOrientation = {
  purpose: string;
  architecture_style: string;
  technologies?: Technology[];
  build?: {
    systems?: {
      system: string;
      files?: string[];
      java_versions?: string[];
    }[];
  };
};
type Workspace = {
  schema_version: string;
  workspace_id: string;
  repository: {
    location: string;
    revision: string;
    source_cached: boolean;
  };
  source_available?: boolean;
  source_status?: "available" | "unavailable" | "restoring" | "failed";
  source_error?: string | null;
  settings: { model: string; reasoning: string };
  status:
    | "queued"
    | "running"
    | "ready"
    | "completed"
    | "completed_static"
    | "awaiting_fallback"
    | "interrupted"
    | "failed";
  phase: string;
  message: string;
  progress: number;
  orientation?: WorkspaceOrientation;
  system_map?: SystemMap;
  main_map_status?: string;
  static_fallback_available?: boolean;
  ai_error?: string;
  components: ComponentSummary[];
  component_jobs?: Record<string, { status: string; message?: string; error?: string }>;
  chat_index?: {
    status: string;
    message?: string;
    strategy?: string;
    chunk_count?: number;
    embedding_model?: string;
    embedding_warning?: string;
    error?: string;
  };
  chat_provider?: {
    provider: string;
    model: string | null;
    auth_mode: string;
  };
  runtime_job_active?: boolean;
  recovery?: {
    state: "none" | "resuming" | "action_required";
    action: "retry_map" | "restart_analysis" | "restore_source" | null;
    reason: string | null;
    attempts: number;
    interrupted_at: string | null;
  };
  updated_at: string;
};
type ComponentArtifact = {
  component_id: string;
  summary: string;
  responsibilities: string[];
  entrypoints: EntryPoint[];
  exit_points: ExitPoint[];
  source: string;
  generation: { provider: string; model: string | null; reasoning: string | null };
  diagram: SystemMap;
};
type ComponentResponse = {
  summary: ComponentSummary;
  status: { status: string; message?: string; error?: string };
  artifact?: ComponentArtifact;
};
type ChatSource = {
  file: string;
  symbol: string;
  start_line?: number | null;
  end_line?: number | null;
  excerpt?: string;
  retrieval_methods?: string[];
};
type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
};
export type ChatState = {
  messages: ChatMessage[];
  question: string;
  status: string;
  error: string;
  sessionId: string | null;
  failedQuestion: string | null;
  warning: string | null;
  scopeExpanded: boolean;
};
type RoutedEdgeData = {
  active: boolean;
  dimmed: boolean;
  selected: boolean;
  step?: number;
  routePoints: ElkPoint[];
};
type GraphMode = "architecture" | "flow";

function confidenceLabel(value: Confidence) {
  return value === "not_determined"
    ? "Not determined"
    : value.charAt(0).toUpperCase() + value.slice(1);
}

function repositoryName(value: string) {
  return value.replace(/\/$/, "").split("/").pop()?.replace(/\.git$/, "") || value;
}

function browserStorage(): Storage | null {
  try {
    const storage = window.localStorage;
    return (
      storage
      && typeof storage.getItem === "function"
      && typeof storage.setItem === "function"
      && typeof storage.removeItem === "function"
    )
      ? storage
      : null;
  } catch {
    return null;
  }
}

async function responseJson(response: Response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || payload.error || "The request could not be completed.";
    throw new Error(String(message));
  }
  return payload;
}

function ConfidenceBadge({ value }: { value: Confidence }) {
  return <span className={`confidence confidence-${value}`}>{confidenceLabel(value)}</span>;
}

function Evidence({
  items,
  label = "Show source proof",
  className = "",
}: {
  items: string[];
  label?: string;
  className?: string;
}) {
  const references = [...new Set(items.filter(Boolean))];
  if (!references.length) return null;
  return (
    <details className={`evidence-disclosure ${className}`.trim()}>
      <summary>{label} <span>{references.length}</span></summary>
      <ul>{references.map((item) => {
        const match = item.match(/^(.*):(\d+(?:-\d+)?)$/);
        return (
          <li key={item}>
            {match ? (
              <>
                <span className="evidence-file">{match[1]}</span>
                <code className="evidence-location">{match[2]}</code>
              </>
            ) : <code className="evidence-raw">{item}</code>}
          </li>
        );
      })}</ul>
    </details>
  );
}

function JourneyAccordions({ flows, edges }: { flows: MapFlow[]; edges: MapEdge[] }) {
  if (!flows.length) {
    return <p className="overview-empty">No connected end-to-end journey was established from repository evidence.</p>;
  }
  return (
    <div className="journey-accordions">
      {flows.map((flow, index) => (
        <details key={flow.id} open={index === 0}>
          <summary>
            <span>{flow.name}</span>
            <ConfidenceBadge value={flow.confidence} />
          </summary>
          <p>{flow.description}</p>
          <dl className="journey-summary">
            <div><dt>Trigger</dt><dd>{flow.trigger}</dd></div>
            <div><dt>Outcome</dt><dd>{flow.outcome}</dd></div>
          </dl>
          <ol>
            {flow.steps.map((step) => {
              const relationship = edges.find((edge) => edge.id === step.edge_id);
              const proof = step.evidence.length ? step.evidence : relationship?.evidence || [];
              return (
                <li key={`${flow.id}-${step.order}`}>
                  <i>{step.order}</i>
                  <div className="journey-step">
                    <strong>{step.from} → {step.to}</strong>
                    <p>{step.action}</p>
                    <small>{step.result}</small>
                    <Evidence
                      items={proof}
                      label="Source proof for this handoff"
                      className="journey-step-proof"
                    />
                  </div>
                </li>
              );
            })}
          </ol>
        </details>
      ))}
    </div>
  );
}

export function RepositoryOverview({
  workspace,
  map,
  onOpenMap,
}: {
  workspace: Workspace;
  map: SystemMap;
  onOpenMap: () => void;
}) {
  const fallbackTechnologies: Technology[] = [
    { name: "Java", category: "language", confidence: "verified", evidence: [] },
    ...(workspace.orientation?.build?.systems || []).map((system) => ({
      name: system.system,
      category: "build" as const,
      confidence: (system.files?.length ? "verified" : "inferred") as Confidence,
      evidence: (system.files || []).slice(0, 3).map((path) => `${path}:1`),
    })),
  ];
  const technologies = workspace.orientation?.technologies?.length
    ? workspace.orientation.technologies
    : fallbackTechnologies;
  return (
    <section className="workspace-overview">
      <div className="overview-hero">
        <div>
          <p className="eyebrow">Repository overview</p>
          <h1>{map.title}</h1>
          <p className="overview-purpose">
            {workspace.orientation?.purpose || map.summary}
          </p>
        </div>
        <button className="primary-button" onClick={onOpenMap}>
          Explore system map <span>→</span>
        </button>
      </div>
      <div className="overview-grid">
        <article className="overview-architecture">
          <p className="eyebrow">Architecture</p>
          <h2>How the system is shaped</h2>
          <p>{workspace.orientation?.architecture_style || map.summary}</p>
          <div className="overview-metrics">
            <div><strong>{workspace.components.length}</strong><span>Top-level components</span></div>
            <div><strong>{map.flows.length}</strong><span>End-to-end flows</span></div>
          </div>
        </article>
        <article className="overview-technologies">
          <p className="eyebrow">Platform</p>
          <h2>Technology stack</h2>
          <div className="technology-list">
            {technologies.map((technology) => (
              <div key={`${technology.category}-${technology.name}`}>
                <span>{technology.category}</span>
                <strong>{technology.name}</strong>
                <ConfidenceBadge value={technology.confidence} />
                <Evidence items={technology.evidence} />
              </div>
            ))}
          </div>
        </article>
      </div>
      <section className="overview-journeys">
        <div>
          <p className="eyebrow">High-level journeys</p>
          <h2>How work moves through the system</h2>
          <p>Open a journey to see its trigger, ordered handoffs, and outcome.</p>
        </div>
        <JourneyAccordions flows={map.flows} edges={map.edges} />
      </section>
    </section>
  );
}

function LoadingLine({ label, progress }: { label: string; progress: number }) {
  return (
    <div className="loading-line" role="status" aria-live="polite">
      <div><span>{label}</span><strong>{Math.max(0, Math.min(progress, 100))}%</strong></div>
      <div className="loading-track"><span style={{ width: `${Math.max(0, Math.min(progress, 100))}%` }} /></div>
    </div>
  );
}

function AnalysisLoading({ workspace }: { workspace: Workspace | null }) {
  const phases = [
    ["cloning", "Prepare source"],
    ["discovering", "Discover Java structure"],
    ["synthesizing", "Trace end-to-end flows"],
    ["completed", "Validate system map"],
  ];
  const phaseIndex = Math.max(0, phases.findIndex(([value]) => value === workspace?.phase));
  return (
    <main className="analysis-screen">
      <div className="analysis-panel">
        <a className="brand" href="#"><span>A</span>ArchAI</a>
        <p className="eyebrow">Building developer intelligence</p>
        <h1>{workspace ? repositoryName(workspace.repository.location) : "Opening repository"}</h1>
        <p className="analysis-message">
          {workspace?.message || "Loading the workspace state."}
        </p>
        <LoadingLine label={workspace?.phase || "starting"} progress={workspace?.progress || 2} />
        <ol className="analysis-stages">
          {phases.map(([value, label], index) => (
            <li
              className={index < phaseIndex ? "done" : index === phaseIndex ? "active" : ""}
              key={value}
            >
              <i>{index < phaseIndex ? "✓" : index + 1}</i><span>{label}</span>
            </li>
          ))}
        </ol>
        <p className="analysis-note">
          The workspace appears only after the system map is complete. ArchAI never builds or runs the repository.
        </p>
      </div>
    </main>
  );
}

type SystemNodeData = MapNode & {
  active: boolean;
  dimmed: boolean;
  selected: boolean;
  step?: number;
  isStart: boolean;
  isExit: boolean;
  direction: "RIGHT" | "DOWN";
};
type BoundaryData = { label: string; kind: string };
type SystemFlowNode = Node<SystemNodeData, "system"> | Node<BoundaryData, "boundary">;

function SystemNode({ data }: NodeProps<Node<SystemNodeData, "system">>) {
  const targetPosition = data.direction === "DOWN" ? Position.Top : Position.Left;
  const sourcePosition = data.direction === "DOWN" ? Position.Bottom : Position.Right;
  return (
    <div className={`system-node kind-${data.kind}${data.active ? " active" : ""}${data.dimmed ? " dimmed" : ""}${data.selected ? " selected" : ""}`}>
      <Handle type="target" position={targetPosition} />
      <div className="node-kicker">
        <span>{data.kind.replace("_", " ")}</span>
        <ConfidenceBadge value={data.confidence} />
      </div>
      <strong>{data.label}</strong>
      <p>{data.summary}</p>
      {data.step && <i className="step-marker">{data.step}</i>}
      {data.isStart && <b className="endpoint-marker start-marker">Start</b>}
      {data.isExit && <b className="endpoint-marker exit-marker">Exit</b>}
      <Handle type="source" position={sourcePosition} />
    </div>
  );
}

function BoundaryNode({ data }: NodeProps<Node<BoundaryData, "boundary">>) {
  return (
    <div className={`boundary-node boundary-${data.kind}`}>
      <span>{data.label}</span>
    </div>
  );
}

const nodeTypes = { system: SystemNode, boundary: BoundaryNode };

function routePath(points: ElkPoint[]) {
  if (!points.length) return "";
  return points
    .map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`)
    .join(" ");
}

function routeMidpoint(points: ElkPoint[]) {
  if (!points.length) return { x: 0, y: 0 };
  if (points.length === 1) return points[0];
  const segments = points.slice(1).map((point, index) => {
    const previous = points[index];
    return {
      from: previous,
      to: point,
      length: Math.hypot(point.x - previous.x, point.y - previous.y),
    };
  });
  const total = segments.reduce((sum, segment) => sum + segment.length, 0);
  let remaining = total / 2;
  for (const segment of segments) {
    if (remaining <= segment.length) {
      const ratio = segment.length ? remaining / segment.length : 0;
      return {
        x: segment.from.x + (segment.to.x - segment.from.x) * ratio,
        y: segment.from.y + (segment.to.y - segment.from.y) * ratio,
      };
    }
    remaining -= segment.length;
  }
  return points[points.length - 1];
}

function RoutedMapEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  data,
}: EdgeProps<Edge<RoutedEdgeData>>) {
  const points = data?.routePoints?.length
    ? data.routePoints
    : [{ x: sourceX, y: sourceY }, { x: targetX, y: targetY }];
  const path = routePath(points);
  const marker = routeMidpoint(points);
  const active = Boolean(data?.active);
  return (
    <>
      {(active || data?.selected) && (
        <BaseEdge
          id={`${id}-halo`}
          path={path}
          style={{ stroke: "white", strokeWidth: data?.selected ? 10 : 8, opacity: 0.98 }}
        />
      )}
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={style}
        interactionWidth={32}
      />
      {data?.step && (
        <EdgeLabelRenderer>
          <div
            className={`edge-step-marker${data.selected ? " selected" : ""}`}
            style={{
              transform: `translate(-50%, -50%) translate(${marker.x}px, ${marker.y}px)`,
            }}
          >
            {data.step}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = { routed: RoutedMapEdge };

export async function layoutSystemMap(
  map: SystemMap,
  activeFlow: MapFlow | undefined,
  selectedNodeId: string | null,
  selectedEdgeId: string | null,
  mode: GraphMode,
  direction: "RIGHT" | "DOWN",
): Promise<{ nodes: SystemFlowNode[]; edges: Edge[] }> {
  const focusedFlow = mode === "flow" ? activeFlow : undefined;
  const flowNodes = new Set(focusedFlow?.node_ids || []);
  const stepByNode = new Map(
    (focusedFlow?.node_ids || []).map((nodeId, index) => [nodeId, index + 1]),
  );
  const stepByEdge = new Map(
    (focusedFlow?.steps || [])
      .filter((step) => step.edge_id)
      .map((step) => [step.edge_id, step.order]),
  );
  const childrenByBoundary = new Map<string, MapNode[]>();
  map.nodes.forEach((node) => {
    const items = childrenByBoundary.get(node.boundary) || [];
    items.push(node);
    childrenByBoundary.set(node.boundary, items);
  });
  const elkGraph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.layered.spacing.nodeNodeBetweenLayers": direction === "RIGHT" ? "132" : "108",
      "elk.spacing.nodeNode": direction === "RIGHT" ? "68" : "54",
      "elk.spacing.edgeNode": "54",
      "elk.spacing.edgeEdge": "26",
      "elk.padding": "[top=36,left=36,bottom=36,right=36]",
    },
    children: map.boundaries.map((boundary) => ({
      id: `boundary:${boundary.id}`,
      layoutOptions: {
        "elk.algorithm": "layered",
        "elk.direction": direction,
        "elk.edgeRouting": "ORTHOGONAL",
        "elk.padding": "[top=58,left=28,bottom=28,right=28]",
        "elk.spacing.nodeNode": "54",
        "elk.spacing.edgeNode": "48",
        "elk.spacing.edgeEdge": "22",
      },
      children: (childrenByBoundary.get(boundary.id) || []).map((node) => ({
        id: node.id,
        width: 272,
        height: 144,
        layoutOptions: {
          "elk.portConstraints": "FIXED_POS",
        },
        ports: direction === "RIGHT"
          ? [
              {
                id: `${node.id}:target`,
                x: 0,
                y: 72,
                width: 0,
                height: 0,
                layoutOptions: { "elk.port.side": "WEST" },
              },
              {
                id: `${node.id}:source`,
                x: 272,
                y: 72,
                width: 0,
                height: 0,
                layoutOptions: { "elk.port.side": "EAST" },
              },
            ]
          : [
              {
                id: `${node.id}:target`,
                x: 136,
                y: 0,
                width: 0,
                height: 0,
                layoutOptions: { "elk.port.side": "NORTH" },
              },
              {
                id: `${node.id}:source`,
                x: 136,
                y: 144,
                width: 0,
                height: 0,
                layoutOptions: { "elk.port.side": "SOUTH" },
              },
            ],
      })),
    })),
    edges: map.edges.map((edge) => ({
      id: edge.id,
      sources: [`${edge.source}:source`],
      targets: [`${edge.target}:target`],
    })),
  };
  const result = await elk.layout(elkGraph);
  const absoluteOffsets = new Map<string, ElkPoint>();
  function recordAbsoluteOffsets(
    node: ElkNode,
    parent: ElkPoint = { x: 0, y: 0 },
  ) {
    const offset = {
      x: parent.x + (node.x || 0),
      y: parent.y + (node.y || 0),
    };
    absoluteOffsets.set(node.id, offset);
    for (const child of node.children || []) {
      recordAbsoluteOffsets(child, offset);
    }
  }
  recordAbsoluteOffsets(result);
  const routedEdges = new Map(
    (result.edges || []).map((edge: ElkExtendedEdge) => [edge.id, edge]),
  );
  const boundariesById = new Map(map.boundaries.map((item) => [item.id, item]));
  const nodes: SystemFlowNode[] = [];
  for (const boundary of result.children || []) {
    const boundaryId = boundary.id.replace(/^boundary:/, "");
    const definition = boundariesById.get(boundaryId);
    nodes.push({
      id: boundary.id,
      type: "boundary",
      position: { x: boundary.x || 0, y: boundary.y || 0 },
      data: {
        label: definition?.label || boundaryId,
        kind: definition?.kind || "application",
      },
      selectable: false,
      draggable: false,
      style: { width: boundary.width || 320, height: boundary.height || 220 },
    });
    for (const child of boundary.children || []) {
      const source = map.nodes.find((item) => item.id === child.id);
      if (!source) continue;
      const step = stepByNode.get(source.id);
      nodes.push({
        id: source.id,
        type: "system",
        parentId: boundary.id,
        extent: "parent",
        position: { x: child.x || 28, y: child.y || 58 },
        data: {
          ...source,
          active: Boolean(focusedFlow && flowNodes.has(source.id)),
          dimmed: Boolean(focusedFlow && !flowNodes.has(source.id)),
          selected: selectedNodeId === source.id,
          step,
          isStart: step === 1,
          isExit: Boolean(step && step === focusedFlow?.node_ids.length),
          direction,
        },
        draggable: false,
        zIndex: 4,
      });
    }
  }
  const edges: Edge[] = map.edges.map((edge) => {
    const active = Boolean(focusedFlow && edge.flow_ids.includes(focusedFlow.id));
    const selected = selectedEdgeId === edge.id;
    const routed = routedEdges.get(edge.id);
    const section = routed?.sections?.[0];
    const containerOffset = absoluteOffsets.get(routed?.container || result.id)
      || { x: 0, y: 0 };
    const routePoints = section
      ? [section.startPoint, ...(section.bendPoints || []), section.endPoint].map(
          (point) => ({
            x: point.x + containerOffset.x,
            y: point.y + containerOffset.y,
          }),
        )
      : [];
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: "routed",
      data: {
        active,
        dimmed: Boolean(focusedFlow && !active),
        selected,
        step: active ? stepByEdge.get(edge.id) : undefined,
        routePoints,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: active || selected ? 22 : 16,
        height: active || selected ? 22 : 16,
        color: active || selected ? "#2367d1" : "#82938d",
      },
      animated: false,
      style: {
        stroke: active || selected ? "#2367d1" : "#82938d",
        strokeWidth: selected ? 4.5 : active ? 3.5 : 1.5,
        opacity: focusedFlow ? (active ? 1 : 0.16) : selected ? 1 : 0.68,
      },
      zIndex: 0,
    };
  });
  return { nodes, edges };
}

function FieldTable({ fields }: { fields: SourceField[] }) {
  if (!fields.length) return <p className="empty-copy">No source-declared fields were found.</p>;
  return (
    <div className="field-table">
      {fields.map((field) => (
        <div key={`${field.name}-${field.line}`}>
          <code>{field.name}</code><span>{field.type}</span><small>{field.required ? "required" : "optional"}</small>
        </div>
      ))}
    </div>
  );
}

function HttpContracts({ contracts }: { contracts: HttpContract[] }) {
  if (!contracts.length) return null;
  return (
    <section className="drawer-section">
      <h4>HTTP contracts</h4>
      {contracts.map((contract) => (
        <details className="http-contract" key={`${contract.handler}-${contract.path}`} open={contracts.length === 1}>
          <summary><span>{contract.methods.join(" | ")}</span><code>{contract.path}</code></summary>
          <p>{contract.framework} · <code>{contract.handler}</code></p>
          <h5>Input</h5>
          {contract.request.parameters.map((parameter) => (
            <div className="contract-parameter" key={`${parameter.name}-${parameter.line}`}>
              <strong>{parameter.name}</strong><span>{parameter.source}</span><code>{parameter.type}</code>
              {!!parameter.fields.length && <FieldTable fields={parameter.fields} />}
            </div>
          ))}
          {!contract.request.parameters.length && <p className="empty-copy">No input parameter was declared.</p>}
          <h5>Output</h5>
          <p><code>{contract.response.type || "void"}</code></p>
          <FieldTable fields={contract.response.fields} />
          <h5>Explicit response codes</h5>
          {contract.response.status_codes.length ? (
            <ul className="status-code-list">
              {contract.response.status_codes.map((status) => (
                <li key={`${status.code}-${status.evidence}`}><strong>{status.code}</strong><span>{status.reason}</span></li>
              ))}
            </ul>
          ) : (
            <p className="empty-copy">Not determined from explicit source evidence.</p>
          )}
          <Evidence items={contract.evidence} />
        </details>
      ))}
    </section>
  );
}

function TextItems({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="drawer-section">
      <h4>{title}</h4>
      <ul className="drawer-list">{items.map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
  );
}

function NodeInspector({
  node,
  onOpenDeepDive,
}: {
  node: MapNode;
  onOpenDeepDive?: (componentId: string) => void;
}) {
  return (
    <div className="drawer-body">
      <div className="drawer-heading">
        <p className="eyebrow">Selected node · {node.kind.replace("_", " ")}</p>
        <h3>{node.label}</h3>
        <ConfidenceBadge value={node.confidence} />
      </div>
      <p className="node-description">{node.description}</p>
      <TextItems title="Responsibilities" items={node.responsibilities} />
      {!!node.entry_points.length && (
        <section className="drawer-section">
          <h4>Entry points</h4>
          <div className="entry-exit-list">
            {node.entry_points.map((entry) => (
              <div key={`${entry.file}-${entry.line}-${entry.label}`}>
                <strong>{entry.label}</strong><span>{entry.kind}</span><code>{entry.file}:{entry.line}</code>
              </div>
            ))}
          </div>
        </section>
      )}
      {!!node.exit_points.length && (
        <section className="drawer-section">
          <h4>Exit points</h4>
          <div className="entry-exit-list">
            {node.exit_points.map((exit) => (
              <div key={`${exit.kind}-${exit.label}`}><strong>{exit.label}</strong><span>{exit.kind}</span><p>{exit.description}</p></div>
            ))}
          </div>
        </section>
      )}
      <TextItems title="Inputs" items={node.inputs} />
      <TextItems title="Outputs" items={node.outputs} />
      <HttpContracts contracts={node.http_contracts} />
      <Evidence items={node.evidence} />
      {node.component_id && onOpenDeepDive && (
        <button className="primary-button drawer-action" onClick={() => onOpenDeepDive(node.component_id!)}>
          Open component deep dive <span>→</span>
        </button>
      )}
    </div>
  );
}

function FlowInspector({ flow }: { flow: MapFlow }) {
  return (
    <div className="drawer-body">
      <div className="drawer-heading">
        <p className="eyebrow">Highlighted flow</p>
        <h3>{flow.name}</h3>
        <ConfidenceBadge value={flow.confidence} />
      </div>
      <p className="node-description">{flow.description}</p>
      <dl className="flow-overview">
        <div><dt>Trigger</dt><dd>{flow.trigger}</dd></div>
        <div><dt>Initial data</dt><dd>{flow.input}</dd></div>
        <div><dt>Outcome</dt><dd>{flow.outcome}</dd></div>
      </dl>
      <ol className="flow-steps">
        {flow.steps.map((step) => (
          <li key={`${step.order}-${step.node_id}`}>
            <i>{step.order}</i>
            <div>
              <strong>{step.from} → {step.to}</strong>
              <p><b>Data:</b> {step.data}</p>
              <p><b>Action:</b> {step.action}</p>
              <p><b>Result:</b> {step.result}</p>
              <Evidence items={step.evidence} />
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function RelationshipInspector({
  edge,
  source,
  target,
  step,
}: {
  edge: MapEdge;
  source?: MapNode;
  target?: MapNode;
  step?: FlowStep;
}) {
  return (
    <div className="drawer-body">
      <div className="drawer-heading">
        <p className="eyebrow">Selected relationship · {edge.kind}</p>
        <h3>{source?.label || edge.source} → {target?.label || edge.target}</h3>
        <ConfidenceBadge value={edge.confidence} />
      </div>
      <p className="node-description">{edge.action || edge.label}</p>
      <dl className="relationship-overview">
        <div><dt>Source</dt><dd>{source?.label || edge.source}</dd></div>
        <div><dt>Destination</dt><dd>{target?.label || edge.target}</dd></div>
        <div><dt>Data transferred</dt><dd>{step?.data || edge.data || "Not determined from repository evidence."}</dd></div>
        <div><dt>Action</dt><dd>{step?.action || edge.action || edge.label}</dd></div>
        <div><dt>Protocol</dt><dd>{edge.protocol || "Not determined from repository evidence."}</dd></div>
        <div><dt>Result</dt><dd>{step?.result || "Not determined for this architectural relationship."}</dd></div>
      </dl>
      <Evidence items={[...edge.evidence, ...(step?.evidence || [])]} />
    </div>
  );
}

async function readSse(
  response: Response,
  onEvent: (payload: Record<string, unknown>) => void,
) {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || payload.error || `Chat request failed (${response.status}).`;
    throw new Error(String(message));
  }
  if (!response.body) throw new Error("The chat response did not include a stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() || "";
    for (const event of events) {
      const data = event.split(/\r?\n/).find((line) => line.startsWith("data: "));
      if (!data) continue;
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(data.slice(6));
      } catch {
        throw new Error("Chat returned a malformed streaming event.");
      }
      onEvent(payload);
      if (payload.type === "complete" || payload.type === "error") terminal = true;
    }
  }
  if (!terminal) throw new Error("Chat ended before ArchAI returned an answer.");
}

export class ChatErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null; retryKey: number }
> {
  state = { error: null as string | null, retryKey: 0 };

  static getDerivedStateFromError(error: Error) {
    return { error: error.message || "The chat drawer could not be displayed." };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="chat-render-error">
          <span>!</span>
          <strong>Chat could not open</strong>
          <p>{this.state.error}</p>
          <button
            type="button"
            onClick={() =>
              this.setState((current) => ({
                error: null,
                retryKey: current.retryKey + 1,
              }))
            }
          >
            Retry chat
          </button>
        </div>
      );
    }
    return (
      <div className="chat-boundary-slot" key={this.state.retryKey}>
        {this.props.children}
      </div>
    );
  }
}

export function WorkspaceChat({
  workspace,
  component,
  state,
  onStateChange,
  requestRef,
  restoreRef,
}: {
  workspace: Workspace;
  component?: ComponentSummary | null;
  state: ChatState;
  onStateChange: React.Dispatch<React.SetStateAction<ChatState>>;
  requestRef: { current: AbortController | null };
  restoreRef: { current: AbortController | null };
}) {
  const [sourceAvailable, setSourceAvailable] = useState(
    workspace.source_available !== false,
  );
  const [sourceStatus, setSourceStatus] = useState(workspace.source_status || "available");
  const [sourceError, setSourceError] = useState(workspace.source_error || "");
  const [gitUsername, setGitUsername] = useState("");
  const [token, setToken] = useState("");
  const messagesRef = useRef<HTMLDivElement>(null);
  const sessionStorageKey = `archai-chat:${workspace.workspace_id}:${component?.id || "repository"}`;

  useEffect(() => {
    const sessionId = browserStorage()?.getItem(sessionStorageKey);
    if (!sessionId) return;
    const controller = new AbortController();
    let cancelled = false;
    fetch(
      `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/chat/sessions/${sessionId}`,
      { signal: controller.signal },
    )
      .then(responseJson)
      .then((session) => {
        if (cancelled || session.component_id !== (component?.id || null)) return;
        const messages: ChatMessage[] = (session.turns || [])
          .filter((turn: Record<string, unknown>) =>
            turn.role === "user" || turn.role === "assistant",
          )
          .map((turn: Record<string, unknown>) => ({
            role: turn.role as "user" | "assistant",
            content: String(turn.content || ""),
            sources: Array.isArray(turn.sources) ? turn.sources as ChatSource[] : undefined,
          }));
        const latestAssistant = [...(session.turns || [])]
          .reverse()
          .find((turn: Record<string, unknown>) => turn.role === "assistant");
        onStateChange((current) => ({
          ...current,
          messages,
          sessionId,
          warning: latestAssistant?.generation_warning
            ? String(latestAssistant.generation_warning)
            : null,
          scopeExpanded: Boolean(latestAssistant?.scope_expanded),
        }));
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        browserStorage()?.removeItem(sessionStorageKey);
        if (error instanceof Error && !error.message.includes("not found")) {
          onStateChange((current) => ({
            ...current,
            error: `Saved conversation could not be restored: ${error.message}`,
          }));
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [component?.id, onStateChange, sessionStorageKey, workspace.workspace_id]);

  useEffect(() => {
    const container = messagesRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [state.messages, state.status, state.error]);

  async function send(value: string, appendUser: boolean) {
    if (!value || state.status || !sourceAvailable) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    onStateChange((current) => ({
      ...current,
      question: appendUser ? "" : current.question,
      error: "",
      failedQuestion: null,
      warning: null,
      scopeExpanded: false,
      messages: appendUser
        ? [...current.messages, { role: "user", content: value }]
        : current.messages,
      status: "Searching repository evidence…",
    }));
    try {
      const response = await fetch(`${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          question: value,
          component_id: component?.id || null,
          session_id: state.sessionId,
        }),
      });
      await readSse(response, (payload) => {
        if (payload.type === "status") {
          if (payload.session_id) {
            browserStorage()?.setItem(sessionStorageKey, String(payload.session_id));
          }
          onStateChange((current) => ({
            ...current,
            status: String(payload.message || "Working…"),
            sessionId: payload.session_id
              ? String(payload.session_id)
              : current.sessionId,
          }));
        }
        if (payload.type === "complete") {
          const sessionId = String(payload.session_id || state.sessionId || "");
          if (sessionId) browserStorage()?.setItem(sessionStorageKey, sessionId);
          const sources = Array.isArray(payload.sources)
            ? payload.sources as ChatSource[]
            : [];
          onStateChange((current) => ({
            ...current,
            messages: [
              ...current.messages,
              {
                role: "assistant",
                content: String(payload.answer || ""),
                sources,
              },
            ],
            sessionId: String(payload.session_id || current.sessionId || ""),
            warning: payload.generation_warning
              ? String(payload.generation_warning)
              : null,
            scopeExpanded: Boolean(payload.scope_expanded),
            status: "",
            error: "",
            failedQuestion: null,
          }));
        }
        if (payload.type === "error") {
          onStateChange((current) => ({
            ...current,
            error: String(payload.error || "Chat failed."),
            status: "",
            failedQuestion: payload.retryable === false ? null : value,
          }));
        }
      });
    } catch (requestError) {
      if (controller.signal.aborted) return;
      onStateChange((current) => ({
        ...current,
        error: requestError instanceof Error ? requestError.message : "Chat failed.",
        status: "",
        failedQuestion: value,
      }));
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = state.question.trim();
    await send(value, true);
  }

  async function clearConversation() {
    requestRef.current?.abort();
    const sessionId = state.sessionId;
    if (sessionId) {
      try {
        await fetch(
          `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/chat/sessions/${sessionId}`,
          { method: "DELETE" },
        );
      } catch {
        // The local conversation can still be reset if server cleanup is unavailable.
      }
    }
    browserStorage()?.removeItem(sessionStorageKey);
    onStateChange({
      messages: [],
      question: "",
      status: "",
      error: "",
      sessionId: null,
      failedQuestion: null,
      warning: null,
      scopeExpanded: false,
    });
  }

  async function restoreSource() {
    restoreRef.current?.abort();
    const controller = new AbortController();
    restoreRef.current = controller;
    setSourceStatus("restoring");
    setSourceError("");
    try {
      const credentials =
        gitUsername.trim() || token
          ? {
              git_username: gitUsername.trim() || null,
              token: token || null,
            }
          : undefined;
      await responseJson(
        await fetch(
          `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/source/restore`,
          {
            method: "POST",
            headers: credentials ? { "Content-Type": "application/json" } : undefined,
            body: credentials ? JSON.stringify(credentials) : undefined,
            signal: controller.signal,
          },
        ),
      );
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const current = await responseJson(
          await fetch(`${API_BASE}/api/v2/workspaces/${workspace.workspace_id}`, {
            signal: controller.signal,
          }),
        );
        if (current.source_available) {
          setSourceAvailable(true);
          setSourceStatus("available");
          setSourceError("");
          setToken("");
          return;
        }
        if (current.source_status === "failed") {
          throw new Error(current.source_error || "Source restoration failed.");
        }
      }
      throw new Error("Source restoration is still running. Try again shortly.");
    } catch (restoreError) {
      if (controller.signal.aborted) return;
      setSourceStatus("failed");
      setSourceError(
        restoreError instanceof Error ? restoreError.message : "Source restoration failed.",
      );
    } finally {
      if (restoreRef.current === controller) restoreRef.current = null;
    }
  }

  return (
    <div className="chat-drawer-content">
      <div className="drawer-heading">
        <p className="eyebrow">{component ? "Component assistant" : "Repository assistant"}</p>
        <div className="chat-heading-row">
          <h3>{component?.display_name || "Whole repository"}</h3>
          {(state.messages.length > 0 || state.sessionId) && (
            <button type="button" onClick={clearConversation}>Clear</button>
          )}
        </div>
        <p className="chat-context">Local Ollama</p>
      </div>
      <div
        aria-label="Chat conversation"
        aria-live="polite"
        className="chat-messages"
        ref={messagesRef}
        role="log"
        tabIndex={0}
      >
        {!sourceAvailable && (
          <div className="source-restore">
            <strong>Repository source is not cached</strong>
            <p>Restore the recorded revision before asking source-grounded questions.</p>
            {sourceStatus === "failed" && (
              <div className="restore-credentials">
                <input
                  value={gitUsername}
                  onChange={(event) => setGitUsername(event.target.value)}
                  placeholder="HTTPS username (optional)"
                />
                <input
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  type="password"
                  placeholder="Access token (optional)"
                />
              </div>
            )}
            <button
              type="button"
              onClick={restoreSource}
              disabled={sourceStatus === "restoring"}
            >
              {sourceStatus === "restoring" ? "Restoring source…" : "Restore source"}
            </button>
            {sourceError && <p className="form-error">{sourceError}</p>}
          </div>
        )}
        {sourceAvailable && !state.messages.length && (
          <div className="chat-empty">
            <strong>Ask about execution, impact, or behavior.</strong>
            <p>Answers stay within this scope and cite source paths and lines.</p>
          </div>
        )}
        {state.messages.map((message, index) => (
          <div className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
            {message.role === "assistant" ? (
              <>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                {message.sources && message.sources.length > 0 && (
                  <details className="chat-sources">
                    <summary>{message.sources.length} source references</summary>
                    {message.sources.map((source, sourceIndex) => (
                      <div className="chat-source" key={`${source.file}-${source.start_line}-${sourceIndex}`}>
                        <strong>
                          {source.file}
                          {source.start_line ? `:${source.start_line}-${source.end_line || source.start_line}` : ""}
                        </strong>
                        <span>{source.symbol}</span>
                        {source.excerpt && <pre>{source.excerpt}</pre>}
                      </div>
                    ))}
                  </details>
                )}
              </>
            ) : <p>{message.content}</p>}
          </div>
        ))}
        {state.scopeExpanded && (
          <p className="chat-scope-note">
            The component search expanded to adjacent repository evidence for this answer.
          </p>
        )}
        {state.warning && <p className="chat-provider-warning">{state.warning}</p>}
        {state.status && <div className="chat-status"><span />{state.status}</div>}
        {state.error && (
          <div className="chat-turn-error">
            <p className="form-error">{state.error}</p>
            {state.failedQuestion && (
              <button
                type="button"
                onClick={() => send(state.failedQuestion!, false)}
                disabled={Boolean(state.status)}
              >
                Retry question
              </button>
            )}
          </div>
        )}
      </div>
      <form className="chat-form" onSubmit={submit}>
        <textarea
          value={state.question}
          onChange={(event) =>
            onStateChange((current) => ({ ...current, question: event.target.value }))
          }
          placeholder={`Ask about ${component?.display_name || "this system"}…`}
          rows={3}
          disabled={Boolean(state.status) || !sourceAvailable}
        />
        <button
          type="submit"
          disabled={!state.question.trim() || Boolean(state.status) || !sourceAvailable}
        >
          Send <span>→</span>
        </button>
      </form>
    </div>
  );
}

export function SystemMapExplorer({
  map,
  workspace,
  component,
  onOpenDeepDive,
}: {
  map: SystemMap;
  workspace: Workspace;
  component?: ComponentSummary | null;
  onOpenDeepDive?: (componentId: string) => void;
}) {
  const initialFlow = map.primary_flow_id || map.flows[0]?.id || "";
  const [activeFlowId, setActiveFlowId] = useState(initialFlow);
  const [mode, setMode] = useState<GraphMode>(initialFlow ? "flow" : "architecture");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<"node" | "relationship" | "flow" | "chat" | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [compactLayout, setCompactLayout] = useState(false);
  const [search, setSearch] = useState("");
  const [layout, setLayout] = useState<{ nodes: SystemFlowNode[]; edges: Edge[] } | null>(null);
  const [chatState, setChatState] = useState<ChatState>({
    messages: [],
    question: "",
    status: "",
    error: "",
    sessionId: null,
    failedQuestion: null,
    warning: null,
    scopeExpanded: false,
  });
  const chatRequestRef = useRef<AbortController | null>(null);
  const sourceRestoreRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      chatRequestRef.current?.abort();
      sourceRestoreRef.current?.abort();
    },
    [],
  );
  const activeFlow = map.flows.find((item) => item.id === activeFlowId);
  const selectedNode = map.nodes.find((item) => item.id === selectedNodeId);
  const selectedEdge = map.edges.find((item) => item.id === selectedEdgeId);
  const edgeSource = map.nodes.find((item) => item.id === selectedEdge?.source);
  const edgeTarget = map.nodes.find((item) => item.id === selectedEdge?.target);
  const selectedEdgeStep = (
    activeFlow?.steps.find((step) => step.edge_id === selectedEdgeId)
    || map.flows.flatMap((flow) => flow.steps).find((step) => step.edge_id === selectedEdgeId)
  );

  useEffect(() => {
    const media = window.matchMedia("(max-width: 900px)");
    const update = () => setCompactLayout(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [expanded]);

  useEffect(() => {
    let cancelled = false;
    void layoutSystemMap(
      map,
      activeFlow,
      selectedNodeId,
      selectedEdgeId,
      mode,
      compactLayout ? "DOWN" : "RIGHT",
    ).then((value) => {
      if (!cancelled) setLayout(value);
    });
    return () => { cancelled = true; };
  }, [map, activeFlow, selectedNodeId, selectedEdgeId, mode, compactLayout]);

  const visibleLayout = useMemo(() => {
    if (!layout || !search.trim()) return layout;
    const query = search.trim().toLowerCase();
    return {
      ...layout,
      nodes: layout.nodes.map((node) => {
        if (node.type !== "system") return node;
        const matches = node.data.label.toLowerCase().includes(query)
          || node.data.summary.toLowerCase().includes(query);
        return { ...node, hidden: !matches };
      }),
      edges: layout.edges.map((edge) => ({ ...edge, hidden: true })),
    };
  }, [layout, search]);

  function selectNode(nodeId: string) {
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
    setDrawer("node");
  }

  function selectEdge(edgeId: string) {
    setSelectedEdgeId(edgeId);
    setSelectedNodeId(null);
    setDrawer("relationship");
  }

  if (!visibleLayout) {
    return <div className="map-layout-loading"><div className="spinner" /><p>Arranging a readable system map…</p></div>;
  }

  const explorer = (
    <div className={`map-explorer${expanded ? " fullscreen-map" : ""}`}>
      <div className="map-toolbar">
        <div className="map-title">
          <p className="eyebrow">{component ? "Focused component map" : "Repository system map"}</p>
          <h1>{map.title}</h1>
          <p>{map.summary}</p>
        </div>
        <div className="map-actions">
          <div className="map-mode-toggle" aria-label="Map mode">
            <button
              className={mode === "architecture" ? "active" : ""}
              onClick={() => setMode("architecture")}
            >
              Architecture
            </button>
            <button
              className={mode === "flow" ? "active" : ""}
              disabled={!activeFlow}
              onClick={() => setMode("flow")}
            >
              Flow focus
            </button>
          </div>
          <label className="map-search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find a node" /></label>
          <label className="flow-select">Execution flow
            <select
              value={activeFlowId}
              onChange={(event) => {
                setActiveFlowId(event.target.value);
                if (event.target.value) {
                  setMode("flow");
                  setDrawer("flow");
                } else {
                  setMode("architecture");
                }
              }}
            >
              <option value="">No selected flow</option>
              {map.flows.map((flow) => <option value={flow.id} key={flow.id}>{flow.name}</option>)}
            </select>
          </label>
          <button
            className="toolbar-button"
            onClick={() => {
              setMode("flow");
              setDrawer(drawer === "flow" ? null : "flow");
            }}
            disabled={!activeFlow}
          >
            Flow details
          </button>
          <button className="toolbar-button" onClick={() => setDrawer(drawer === "chat" ? null : "chat")}>Ask ArchAI</button>
          <button
            className="expand-button"
            autoFocus={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Close full screen" : "Full screen"}
          </button>
        </div>
      </div>
      {mode === "flow" && activeFlow ? (
        <button className="flow-summary" onClick={() => setDrawer("flow")}>
          <span>{activeFlow.id === map.primary_flow_id ? "Primary path" : "Selected path"}</span>
          <strong>{activeFlow.name}</strong><p>{activeFlow.description}</p><i>View {activeFlow.steps.length} explained steps →</i>
        </button>
      ) : !map.flows.length ? (
        <div className="no-flow-note"><strong>No complete execution path was established.</strong><span>Showing evidence-backed architecture relationships only.</span></div>
      ) : (
        <div className="architecture-note"><strong>Architecture mode</strong><span>Select a relationship for its complete handoff, or switch to Flow Focus to trace an execution path.</span></div>
      )}
      <div className={`map-stage${drawer ? " drawer-open" : ""}`}>
        <div className="react-flow-frame">
          <ReactFlow
            key={`${map.title}-${activeFlowId}-${mode}-${compactLayout}-${expanded}`}
            nodes={visibleLayout.nodes}
            edges={visibleLayout.edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            fitViewOptions={{ padding: 0.06, minZoom: 0.37, maxZoom: 1.2 }}
            minZoom={0.18}
            maxZoom={1.8}
            nodesDraggable={false}
            nodesConnectable={false}
            onNodeClick={(_, node) => {
              if (node.type === "system") selectNode(node.id);
            }}
            onEdgeClick={(_, edge) => selectEdge(edge.id)}
            onPaneClick={() => {
              setSelectedNodeId(null);
              setSelectedEdgeId(null);
              if (drawer === "node" || drawer === "relationship") setDrawer(null);
            }}
            proOptions={{ hideAttribution: false }}
          >
            <Background gap={24} size={1} color="#d9e3df" />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        {drawer && (
          <aside className="map-drawer">
            <div className="drawer-tabs">
              <button className={drawer === "node" ? "active" : ""} disabled={!selectedNode} onClick={() => setDrawer("node")}>Node</button>
              <button className={drawer === "relationship" ? "active" : ""} disabled={!selectedEdge} onClick={() => setDrawer("relationship")}>Relationship</button>
              <button className={drawer === "flow" ? "active" : ""} disabled={!activeFlow} onClick={() => setDrawer("flow")}>Flow</button>
              <button className={drawer === "chat" ? "active" : ""} onClick={() => setDrawer("chat")}>Chat</button>
              <button className="drawer-close" onClick={() => setDrawer(null)} aria-label="Close drawer">×</button>
            </div>
            {drawer === "node" && selectedNode && <NodeInspector node={selectedNode} onOpenDeepDive={onOpenDeepDive} />}
            {drawer === "relationship" && selectedEdge && (
              <RelationshipInspector
                edge={selectedEdge}
                source={edgeSource}
                target={edgeTarget}
                step={selectedEdgeStep}
              />
            )}
            {drawer === "flow" && activeFlow && <FlowInspector flow={activeFlow} />}
            {drawer === "chat" && (
              <ChatErrorBoundary>
                <WorkspaceChat
                  workspace={workspace}
                  component={component}
                  state={chatState}
                  onStateChange={setChatState}
                  requestRef={chatRequestRef}
                  restoreRef={sourceRestoreRef}
                />
              </ChatErrorBoundary>
            )}
          </aside>
        )}
      </div>
    </div>
  );
  if (!expanded) return explorer;
  return createPortal(
    <div
      className="map-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setExpanded(false);
      }}
    >
      <section
        className="map-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${map.title} full screen map`}
      >
        {explorer}
      </section>
    </div>,
    document.body,
  );
}

export function ComponentDeepDive({
  workspace,
  component,
  data,
  loading,
  onBack,
}: {
  workspace: Workspace;
  component: ComponentSummary;
  data: ComponentResponse | null;
  loading: boolean;
  onBack: () => void;
}) {
  const terminal = data && ["completed", "completed_static", "failed"].includes(data.status.status);
  if (loading || !terminal) {
    return (
      <main className="component-wait">
        <button className="back-button" onClick={onBack}>← Back to system map</button>
        <div className="spinner" />
        <h1>Preparing {component.display_name}</h1>
        <p>{data?.status.message || "Prioritizing this component’s execution map."}</p>
      </main>
    );
  }
  if (!data?.artifact || data.status.status === "failed") {
    return (
      <main className="fatal-state">
        <button className="back-button" onClick={onBack}>← Back to system map</button>
        <h1>Component map unavailable</h1><p>{data?.status.error || "No completed component artifact is available."}</p>
      </main>
    );
  }
  return (
    <div className="deep-dive">
      <header className="deep-header">
        <button className="back-button" onClick={onBack}>← Back to system map</button>
        <div><span>{component.kind}</span><h1>{component.display_name}</h1><p>{data.artifact.summary}</p></div>
        <small>Static source evidence</small>
      </header>
      <SystemMapExplorer map={data.artifact.diagram} workspace={workspace} component={component} />
    </div>
  );
}

export function RepositoryWorkspace({
  workspace,
  onClose,
  onOpenDeepDive,
}: {
  workspace: Workspace;
  onClose: () => void;
  onOpenDeepDive: (componentId: string) => void;
}) {
  const [view, setView] = useState<"overview" | "map">("overview");
  const map = workspace.system_map!;
  return (
    <main className={`workspace-shell workspace-${view}`}>
      <header className="workspace-header">
        <button className="brand brand-button" onClick={onClose}><span>A</span>ArchAI</button>
        <div className="repository-title"><strong>{repositoryName(workspace.repository.location)}</strong></div>
        <button className="secondary-button" onClick={onClose}>Analyze another</button>
      </header>
      <nav className="workspace-tabs" aria-label="Repository workspace views">
        <button className={view === "overview" ? "active" : ""} onClick={() => setView("overview")}>Overview</button>
        <button className={view === "map" ? "active" : ""} onClick={() => setView("map")}>System Map</button>
      </nav>
      {view === "overview" ? (
        <RepositoryOverview workspace={workspace} map={map} onOpenMap={() => setView("map")} />
      ) : (
        <SystemMapExplorer map={map} workspace={workspace} onOpenDeepDive={onOpenDeepDive} />
      )}
    </main>
  );
}

export function RepositoryForm({
  onCreated,
  recent,
}: {
  onCreated: (id: string) => void;
  recent: Workspace[];
}) {
  const [repository, setRepository] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const payload = await responseJson(await fetch(`${API_BASE}/api/v2/workspaces`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repository: repository.trim(),
          token: token || null,
          recursive: true,
          reasoning: "high",
        }),
      }));
      setToken("");
      onCreated(payload.workspace_id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Repository analysis could not start.");
      setToken("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="landing">
      <header className="landing-header"><a className="brand" href="#"><span>A</span>ArchAI</a><p>Developer intelligence for Java systems</p></header>
      <section className="landing-hero">
        <div className="hero-copy">
          <p className="eyebrow">Understand execution before changing code</p>
          <h1>Turn a Java repository into an explorable system map.</h1>
          <p>Follow real entry-to-exit flows, inspect what each component receives and produces, and ask grounded questions without running the repository.</p>
          <div className="value-list">
            <div><span>01</span><p><strong>Know where to start</strong>A primary evidence-backed flow is selected and numbered.</p></div>
            <div><span>02</span><p><strong>Inspect every handoff</strong>See data, action, destination, output, and source proof.</p></div>
            <div><span>03</span><p><strong>Go deeper when needed</strong>Open focused component maps and scoped chat without losing context.</p></div>
          </div>
        </div>
        <form className="connect-card" onSubmit={submit}>
          <div><p className="eyebrow">Create a workspace</p><h2>Connect a repository</h2><p>HTTPS and SSH Git URLs are supported.</p></div>
          <label>Repository URL<input value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="ssh://git@example.com:2222/team/project.git" required /></label>
          <label>HTTPS access token <span>Optional</span><input type="password" value={token} onChange={(event) => setToken(event.target.value)} /></label>
          {error && <p className="form-error">{error}</p>}
          <button className="primary-button" type="submit" disabled={!repository.trim() || submitting}>{submitting ? "Starting analysis…" : "Analyze repository"}<span>→</span></button>
          <p className="security-copy">ArchAI never runs the repository’s build, tests, or application.</p>
        </form>
      </section>
      {!!recent.length && <section className="recent-workspaces"><p className="eyebrow">Recent workspaces</p><div>{recent.slice(0, 4).map((item) => <button key={item.workspace_id} onClick={() => onCreated(item.workspace_id)}><strong>{repositoryName(item.repository.location)}</strong><span>{item.status}</span><i>→</i></button>)}</div></section>}
    </main>
  );
}

function FallbackChoice({
  workspace,
  onUpdated,
  onClose,
}: {
  workspace: Workspace;
  onUpdated: (workspace: Workspace | null) => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function action(path: "retry" | "use-static") {
    setBusy(true);
    setError("");
    try {
      const payload = await responseJson(await fetch(`${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/map/${path}`, { method: "POST" }));
      onUpdated(path === "retry" ? null : payload);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The action failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="fallback-screen">
      <div className="fallback-card">
        <span className="warning-mark">!</span>
        <p className="eyebrow">System-map recovery</p>
        <h1>A source-backed map is ready</h1>
        <p>{workspace.ai_error || workspace.message}</p>
        <div className="fallback-actions">
          <button className="primary-button" disabled={busy} onClick={() => action("use-static")}>Open source-backed map</button>
          <button className="secondary-button" disabled={busy} onClick={() => action("retry")}>Rebuild source map</button>
          <button className="text-button" disabled={busy} onClick={onClose}>Choose another repository</button>
        </div>
        {error && <p className="form-error">{error}</p>}
      </div>
    </main>
  );
}

export function InterruptedChoice({
  workspace,
  onUpdated,
  onClose,
}: {
  workspace: Workspace;
  onUpdated: (workspace: Workspace | null) => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [gitUsername, setGitUsername] = useState("");
  const [token, setToken] = useState("");
  const action = workspace.recovery?.action || "restart_analysis";

  async function recover(path: "retry_map" | "restart_analysis" | "restore_source") {
    setBusy(true);
    setError("");
    try {
      if (path === "retry_map") {
        await responseJson(
          await fetch(
            `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/map/retry`,
            { method: "POST" },
          ),
        );
      } else {
        const credentials =
          gitUsername.trim() || token
            ? {
                git_username: gitUsername.trim() || null,
                token: token || null,
              }
            : undefined;
        const endpoint =
          path === "restore_source"
            ? "source/restore"
            : "refresh";
        await responseJson(
          await fetch(
            `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/${endpoint}`,
            {
              method: "POST",
              headers: credentials ? { "Content-Type": "application/json" } : undefined,
              body: credentials ? JSON.stringify(credentials) : undefined,
            },
          ),
        );
      }
      setToken("");
      onUpdated(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Analysis recovery failed.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="fallback-screen">
      <div className="fallback-card recovery-card">
        <span className="warning-mark">!</span>
        <p className="eyebrow">Analysis interrupted</p>
        <h1>Your saved repository evidence is safe</h1>
        <p>{workspace.recovery?.reason || workspace.message}</p>
        {action !== "retry_map" && (
          <div className="recovery-credentials">
            <p>For a private repository, enter credentials again. ArchAI does not persist them.</p>
            <label>
              HTTPS username <span>Optional</span>
              <input
                value={gitUsername}
                onChange={(event) => setGitUsername(event.target.value)}
                placeholder="Git username"
              />
            </label>
            <label>
              Access token <span>Optional</span>
              <input
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Not stored by ArchAI"
              />
            </label>
          </div>
        )}
        <div className="fallback-actions">
          <button
            className="primary-button"
            disabled={busy}
            onClick={() => recover(action)}
          >
            {action === "retry_map"
              ? "Resume map synthesis"
              : action === "restore_source"
                ? "Restore source"
                : "Restart analysis"}
          </button>
          {workspace.static_fallback_available && (
            <button
              className="secondary-button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  const payload = await responseJson(
                    await fetch(
                      `${API_BASE}/api/v2/workspaces/${workspace.workspace_id}/map/use-static`,
                      { method: "POST" },
                    ),
                  );
                  onUpdated(payload);
                } catch (requestError) {
                  setError(
                    requestError instanceof Error
                      ? requestError.message
                      : "The static map could not be opened.",
                  );
                } finally {
                  setBusy(false);
                }
              }}
            >
              Open static map
            </button>
          )}
          <button className="text-button" disabled={busy} onClick={onClose}>
            Choose another repository
          </button>
        </div>
        {error && <p className="form-error">{error}</p>}
      </div>
    </main>
  );
}

export default function Home() {
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [recent, setRecent] = useState<Workspace[]>([]);
  const [selectedComponentId, setSelectedComponentId] = useState<string | null>(null);
  const [componentData, setComponentData] = useState<ComponentResponse | null>(null);
  const [componentLoading, setComponentLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const linkedWorkspace = params.get("workspace");
    const linkedComponent = params.get("component");
    const timer = window.setTimeout(() => {
      if (linkedWorkspace) setWorkspaceId(linkedWorkspace);
      if (linkedWorkspace && linkedComponent) setSelectedComponentId(linkedComponent);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/api/v2/workspaces`).then(responseJson).then((payload) => setRecent(payload.workspaces || [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout>;
    let events: EventSource | null = null;

    function applyWorkspace(payload: Workspace) {
      if (cancelled) return;
      setWorkspace(payload);
      setError("");
      if (
        ["completed", "completed_static", "awaiting_fallback", "interrupted", "failed"]
          .includes(payload.status)
      ) {
        events?.close();
        events = null;
      }
    }

    function connectEvents() {
      if (cancelled || events) return;
      events = new EventSource(
        `${API_BASE}/api/v2/workspaces/${workspaceId}/events`,
      );
      events.onmessage = (event) => {
        try {
          applyWorkspace(JSON.parse(event.data) as Workspace);
        } catch {
          setError("Workspace updates could not be read.");
        }
      };
      events.onerror = () => {
        events?.close();
        events = null;
        if (!cancelled) retryTimer = setTimeout(load, 2500);
      };
    }

    async function load() {
      try {
        const payload = await responseJson(await fetch(`${API_BASE}/api/v2/workspaces/${workspaceId}`));
        if (cancelled) return;
        applyWorkspace(payload);
        if (
          !["completed", "completed_static", "awaiting_fallback", "interrupted", "failed"]
            .includes(payload.status)
        ) {
          connectEvents();
        }
      } catch (requestError) {
        if (!cancelled) {
          setError(requestError instanceof Error ? requestError.message : "Workspace could not be loaded.");
          retryTimer = setTimeout(load, 2500);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      events?.close();
    };
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId || !selectedComponentId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function pollComponent() {
      try {
        const payload = await responseJson(await fetch(`${API_BASE}/api/v2/workspaces/${workspaceId}/components/${selectedComponentId}`));
        if (cancelled) return;
        setComponentData(payload);
        const running = ["not_started", "queued", "running"].includes(payload.status?.status);
        setComponentLoading(running);
        if (running) timer = setTimeout(pollComponent, 2000);
      } catch {
        if (!cancelled) setComponentLoading(false);
      }
    }
    fetch(`${API_BASE}/api/v2/workspaces/${workspaceId}/components/${selectedComponentId}/generate`, { method: "POST" })
      .then(() => pollComponent())
      .catch(() => setComponentLoading(false));
    return () => { cancelled = true; clearTimeout(timer); };
  }, [workspaceId, selectedComponentId]);

  function openWorkspace(id: string) {
    setWorkspaceId(id);
    setWorkspace(null);
    setSelectedComponentId(null);
    setComponentData(null);
    window.history.replaceState(null, "", `?${new URLSearchParams({ workspace: id }).toString()}`);
  }

  function closeWorkspace() {
    setWorkspaceId(null);
    setWorkspace(null);
    setSelectedComponentId(null);
    setComponentData(null);
    window.history.replaceState(null, "", window.location.pathname);
  }

  function openDeepDive(componentId: string) {
    setSelectedComponentId(componentId);
    setComponentData(null);
    setComponentLoading(true);
    if (workspaceId) {
      window.history.replaceState(null, "", `?${new URLSearchParams({ workspace: workspaceId, component: componentId }).toString()}`);
    }
  }

  function closeDeepDive() {
    setSelectedComponentId(null);
    setComponentData(null);
    setComponentLoading(false);
    if (workspaceId) {
      window.history.replaceState(null, "", `?${new URLSearchParams({ workspace: workspaceId }).toString()}`);
    }
  }

  if (!workspaceId) return <RepositoryForm onCreated={openWorkspace} recent={recent} />;
  if (!workspace || ["queued", "running", "ready"].includes(workspace.status)) return <AnalysisLoading workspace={workspace} />;
  if (workspace.status === "interrupted") {
    return <InterruptedChoice workspace={workspace} onUpdated={setWorkspace} onClose={closeWorkspace} />;
  }
  if (workspace.status === "awaiting_fallback") {
    return <FallbackChoice workspace={workspace} onUpdated={setWorkspace} onClose={closeWorkspace} />;
  }
  if (workspace.status === "failed") {
    return <main className="fatal-state"><h1>Repository analysis failed</h1><p>{workspace.message}</p><code>{error}</code><button className="primary-button" onClick={closeWorkspace}>Choose another repository</button></main>;
  }
  if (!workspace.system_map) {
    return <main className="fatal-state"><h1>System map unavailable</h1><p>The workspace completed without a readable map artifact.</p><button className="primary-button" onClick={closeWorkspace}>Choose another repository</button></main>;
  }
  const selectedComponent = workspace.components.find((item) => item.id === selectedComponentId) || null;
  if (selectedComponent) {
    return <ComponentDeepDive workspace={workspace} component={selectedComponent} data={componentData} loading={componentLoading} onBack={closeDeepDive} />;
  }
  return (
    <RepositoryWorkspace
      workspace={workspace}
      onClose={closeWorkspace}
      onOpenDeepDive={openDeepDive}
    />
  );
}
