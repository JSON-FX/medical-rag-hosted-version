import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Medical RAG — grounded answers with citations",
  description:
    "Answers drawn only from three FDA drug labels, cited to the page, with the retrieval "
    + "and confidence-gate telemetry shown for every question.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
