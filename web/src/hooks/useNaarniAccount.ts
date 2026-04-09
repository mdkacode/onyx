import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";

interface NaarniAuthStatus {
  connected: boolean;
  phone_number: string | null;
}

export default function useNaarniAccount() {
  const { data, error, isLoading, mutate } = useSWR<NaarniAuthStatus>(
    "/api/naarni-auth/status",
    errorHandlingFetcher
  );

  return {
    isConnected: data?.connected ?? false,
    phoneNumber: data?.phone_number ?? null,
    isLoading,
    error,
    refetch: mutate,
  };
}
