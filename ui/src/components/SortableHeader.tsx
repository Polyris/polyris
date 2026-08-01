import { ArrowUp, ArrowDown } from '../utils/icons';

interface SortableHeaderProps {
    label: string;
    sortKey: string;
    currentSort: { key: string; dir: string };
    onSort: (key: string) => void;
    className?: string;
}

/**
 * SortableHeader - Clickable column header with sort indicator and keyboard support
 */
export function SortableHeader({ label, sortKey, currentSort, onSort, className = 'table-cell' }: SortableHeaderProps) {
    const isActive = currentSort.key === sortKey;
    const ariaSort: 'ascending' | 'descending' | undefined = isActive ? (currentSort.dir === 'asc' ? 'ascending' : 'descending') : undefined;
    return (
        <th 
            className={`${className} sh-sortable-header cursor-pointer select-none`}
            onClick={() => onSort(sortKey)}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSort(sortKey); } }}
            scope="col"
            aria-sort={ariaSort}
            tabIndex={0}
            role="columnheader"
        >
            <span className="inline-flex items-center gap-1">
                {label}
                {isActive ? (
                    currentSort.dir === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />
                ) : (
                    <span className="opacity-25 text-[10px]">⇅</span>
                )}
            </span>
        </th>
    );
}
