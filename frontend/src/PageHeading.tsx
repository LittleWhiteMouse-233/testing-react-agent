import type { ReactNode } from "react";

export default function PageHeading({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return <header className="page-heading"><div>{children}</div>{actions && <div className="heading-actions">{actions}</div>}</header>;
}
