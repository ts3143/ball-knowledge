import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "Ball Knowledge",
  description: "A daily sports trivia dive.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
