/**
 * AWS-style service badge for a task's compute type.
 *
 * Shows the task's AWS service by its brand colour and short name, so the type is
 * recognizable on a DAG node (or task row) without opening the task modal. Colours
 * follow the AWS Architecture-icon category palette — compute is orange, analytics
 * is purple, application-integration is magenta — which is why several services
 * share a colour and the label disambiguates them. This is a local, text-plus-colour
 * badge; it deliberately does not embed AWS's own icon artwork.
 */
export interface TaskTypeBadge {
    /** Short uppercase service label, e.g. 'LAMBDA'. */
    label: string;
    /** AWS service brand colour used as the badge background. */
    color: string;
}

const TASK_TYPE_BADGES: Record<string, TaskTypeBadge> = {
    // Compute — orange
    lambda: { label: 'LAMBDA', color: '#ED7100' },
    ecs: { label: 'ECS', color: '#ED7100' },
    batch: { label: 'BATCH', color: '#ED7100' },
    // Analytics — purple
    glue: { label: 'GLUE', color: '#8C4FFF' },
    athena: { label: 'ATHENA', color: '#8C4FFF' },
    emr: { label: 'EMR', color: '#8C4FFF' },
    // Application integration — magenta
    sfn: { label: 'SFN', color: '#E7157B' },
};

/** Return the AWS-style badge for a task type, or null for unknown/absent types. */
export function taskTypeBadge(taskType?: string | null): TaskTypeBadge | null {
    if (!taskType) return null;
    return TASK_TYPE_BADGES[taskType.toLowerCase()] ?? null;
}
