import { useState, useEffect } from "react";
import {
  ConfigurableSources,
  FederatedConnectorDetail,
  CredentialSchemaResponse,
  federatedSourceToRegularSource,
  ValidSources,
} from "@/lib/types";

interface UseFederatedConnectorResult {
  sourceType: ConfigurableSources | null;
  connectorData: FederatedConnectorDetail | null;
  credentialSchema: CredentialSchemaResponse | null;
  isLoading: boolean;
  error: string | null;
}

export function useFederatedConnector(
  connectorId: string
): UseFederatedConnectorResult {
  const [sourceType, setSourceType] = useState<ConfigurableSources | null>(
    null
  );
  const [connectorData, setConnectorData] =
    useState<FederatedConnectorDetail | null>(null);
  const [credentialSchema, setCredentialSchema] =
    useState<CredentialSchemaResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        setIsLoading(true);
        setError(null);

        // First, fetch connector details to get the source type
        const connectorResponse = await fetch(`/api/federated/${connectorId}`);

        if (!connectorResponse.ok) {
          throw new Error(
            `Failed to fetch connector: ${connectorResponse.statusText}`
          );
        }

        const connectorData: FederatedConnectorDetail =
          await connectorResponse.json();

        // Map federated source to its base configurable source
        const extractedSourceType = federatedSourceToRegularSource(
          connectorData.source as ValidSources
        ) as ConfigurableSources;

        // Now fetch credential schema using the original federated source name
        const schemaPromise = fetch(
          `/api/federated/sources/${connectorData.source}/credentials/schema`
        );

        // Set the data we already have
        setConnectorData(connectorData);
        setSourceType(extractedSourceType);

        // Wait for schema fetch to complete
        const schemaResponse = await schemaPromise;

        if (!schemaResponse.ok) {
          throw new Error(
            `Failed to fetch schema: ${schemaResponse.statusText}`
          );
        }

        const schemaData: CredentialSchemaResponse =
          await schemaResponse.json();
        setCredentialSchema(schemaData);
      } catch (error) {
        console.error("Error fetching federated connector data:", error);
        setError(`Failed to load connector: ${error}`);
      } finally {
        setIsLoading(false);
      }
    };

    if (connectorId) {
      fetchData();
    }
  }, [connectorId]);

  return {
    sourceType,
    connectorData,
    credentialSchema,
    isLoading,
    error,
  };
}
