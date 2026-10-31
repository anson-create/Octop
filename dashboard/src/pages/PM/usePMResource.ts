import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../utils/apiError";

/**
 * Minimal resource loader for PM pages.
 *
 * The dashboard's ``useAsyncResource`` swallows errors into a toast; the PM
 * pages need the failure inline (a 503 means "this instance has no PM
 * workspace", which must not look like an empty list).
 */
export function usePMResource<T>(
  initial: T,
  fetcher: () => Promise<T>,
  deps: unknown[],
  options?: { enabled?: boolean },
) {
  const { t } = useTranslation();
  const [data, setData] = useState<T>(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = options?.enabled !== false;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const initialRef = useRef(initial);
  initialRef.current = initial;
  const fallback = t("pm.error.load");

  const refresh = useCallback(async () => {
    if (!enabled) {
      setData(initialRef.current);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setData(await fetcherRef.current());
    } catch (err) {
      setError(apiErrorMessage(err, fallback, t));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, t, ...deps]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { data, loading, error, refresh, setData };
}
