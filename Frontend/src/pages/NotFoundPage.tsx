import { Link } from 'react-router-dom';
import { ArrowLeft, Search } from 'lucide-react';
import { useSearchPreservingPath } from '@/state/urlState';
import { Button } from '@/components/ui/button';

export function NotFoundPage({ what = 'page' }: { what?: string }) {
  const preserve = useSearchPreservingPath();

  return (
    <div className="mx-auto flex min-h-[60vh] w-full max-w-2xl flex-col items-start justify-center gap-5 px-4 py-16 sm:px-6">
      <div className="app-kicker">404 · Not found</div>
      <h1 className="m-0 text-3xl font-bold tracking-tight sm:text-[40px] sm:leading-[0.95]">
        That {what} is not in this field
      </h1>
      <p className="m-0 max-w-xl text-sm leading-relaxed text-mist">
        Nothing was found at this address. It may have been removed, or the link may be wrong.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button asChild>
          <Link to={preserve('/')} className="no-underline">
            <ArrowLeft className="size-4" aria-hidden="true" />
            Back to the sky explorer
          </Link>
        </Button>
        <Button variant="outline" asChild>
          <Link to={preserve('/candidates')} className="no-underline">
            <Search className="size-4" aria-hidden="true" />
            Open candidate search
          </Link>
        </Button>
      </div>
    </div>
  );
}
