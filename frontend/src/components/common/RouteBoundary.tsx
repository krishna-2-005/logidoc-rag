import { Component, Suspense, type ReactNode } from "react"
import { ErrorState, LoadingBlock } from "@/components/common/states"

/**
 * Around a lazily loaded page: a loading state while its code arrives, and an
 * error when it cannot be loaded (offline, or the server was updated since
 * this tab was opened). Retrying reloads the application, which also fetches
 * the current build: a failed import is not fetched again in place. The shell
 * stays usable either way.
 */
export function RouteBoundary({ children, reload = () => window.location.reload() }: { children: ReactNode; reload?: () => void }) {
  return (
    <PageErrorBoundary reload={reload}>
      <Suspense fallback={<LoadingBlock rows={3} label="Loading the page" />}>{children}</Suspense>
    </PageErrorBoundary>
  )
}

class PageErrorBoundary extends Component<{ children: ReactNode; reload: () => void }, { error: unknown }> {
  state = { error: null as unknown }

  static getDerivedStateFromError(error: unknown) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return <ErrorState error={this.state.error} title="This page could not be loaded" onRetry={this.props.reload} />
    }

    return this.props.children
  }
}
