import { useClientRoute } from '@/hooks/useClientRoute';
import { viewFromPathname } from '../utils';
import { paidSurface } from '@/ee-active.generated';
import { ViewTab } from './ViewTab';
import { UserMenu } from './UserMenu';
import { Workflow, Package, Activity } from 'lucide-react';

/**
 * AppNav — the left rail of the app shell: brand mark plus primary navigation.
 *
 * Primary navigation belongs in the shell (per the redesign brief); contextual
 * breadcrumbs and global controls stay in the topbar (Header). Active view is
 * derived from the pathname, matching Header, and Team-only destinations (Assets,
 * Backfills) render only when the paid surface provides them — the OSS build ships
 * neither the views nor their nav entries.
 */
export function AppNav() {
    const { pathname, push } = useClientRoute();
    const mainView = viewFromPathname(pathname);
    const switchView = (view: string) => push(`/${view}/`);

    const BackfillNavTab = paidSurface.BackfillNavTab;
    const AssetsView = paidSurface.AssetsView;

    return (
        <nav className="app-rail" aria-label="Primary navigation">
            <div className="app-rail-nav" role="tablist">
                <ViewTab
                    active={mainView === 'pipelines'}
                    onClick={() => switchView('pipelines')}
                    icon={<Workflow size={20} />}
                    label="Pipelines"
                />
                {AssetsView && (
                    <ViewTab
                        active={mainView === 'assets'}
                        onClick={() => switchView('assets')}
                        icon={<Package size={20} />}
                        label="Assets"
                    />
                )}
                <ViewTab
                    active={mainView === 'runs' || mainView === 'tasks'}
                    onClick={() => switchView('runs')}
                    icon={<Activity size={20} />}
                    label="History"
                />
                {BackfillNavTab && (
                    <BackfillNavTab
                        active={mainView === 'backfills'}
                        onClick={() => switchView('backfills')}
                    />
                )}
            </div>

            <div className="app-rail-account">
                <UserMenu />
            </div>
        </nav>
    );
}

export default AppNav;
