declare module 'dagre' {
  export interface GraphLabel {
    rankdir?: string;
    nodesep?: number;
    ranksep?: number;
    marginx?: number;
    marginy?: number;
    width?: number;
    height?: number;
  }
  export interface NodeConfig {
    width?: number;
    height?: number;
    [key: string]: unknown;
  }
  export class graphlib {
    static Graph: new (opts?: { directed?: boolean; compound?: boolean; multigraph?: boolean }) => Graph;
  }
  export interface Graph {
    setDefaultEdgeLabel(fn: () => Record<string, unknown>): void;
    setGraph(label: GraphLabel): void;
    graph(): GraphLabel;
    setNode(id: string, config: NodeConfig): void;
    setEdge(source: string, target: string): void;
    node(id: string): NodeConfig & { x: number; y: number };
  }
  export function layout(g: Graph): void;
}
