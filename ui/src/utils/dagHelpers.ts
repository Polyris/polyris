/**
 * DAG Helper Functions
 * 
 * Utilities for traversing and analyzing DAG structure.
 * Single source of truth for upstream/downstream task calculations.
 */

import type { DAG } from '../types';

/**
 * Get all upstream tasks (dependencies) for a given task using BFS.
 */
export function getUpstreamTasks(taskName: string, dag: DAG | null): string[] {
    if (!dag?.edges) return [];
    
    const upstream = new Set<string>();
    const queue = [taskName];
    
    while (queue.length > 0) {
        const current = queue.shift();
        dag.edges
            .filter(e => e.to === current)
            .forEach(e => {
                if (!upstream.has(e.from)) {
                    upstream.add(e.from);
                    queue.push(e.from);
                }
            });
    }
    
    return Array.from(upstream);
}

/**
 * Get all downstream tasks (dependents) for a given task using BFS.
 */
export function getDownstreamTasks(taskName: string, dag: DAG | null): string[] {
    if (!dag?.edges) return [];
    
    const downstream = new Set<string>();
    const queue = [taskName];
    
    while (queue.length > 0) {
        const current = queue.shift();
        dag.edges
            .filter(e => e.from === current)
            .forEach(e => {
                if (!downstream.has(e.to)) {
                    downstream.add(e.to);
                    queue.push(e.to);
                }
            });
    }
    
    return Array.from(downstream);
}

/**
 * Get count of upstream tasks.
 */
export function getUpstreamCount(taskName: string, dag: DAG | null): number {
    return getUpstreamTasks(taskName, dag).length;
}

/**
 * Get count of downstream tasks.
 */
export function getDownstreamCount(taskName: string, dag: DAG | null): number {
    return getDownstreamTasks(taskName, dag).length;
}
