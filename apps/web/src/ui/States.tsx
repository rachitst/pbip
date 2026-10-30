import { Button } from "./Button";

export function LoadingState({ label }: { label: string }) {
  return (
    <p className="state" role="status">
      {label}
    </p>
  );
}

export function EmptyState({ message }: { message: string }) {
  return <p className="state">{message}</p>;
}

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <p className="state state--error" role="alert">
      {message}
      {onRetry && (
        <Button variant="ghost" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </p>
  );
}
