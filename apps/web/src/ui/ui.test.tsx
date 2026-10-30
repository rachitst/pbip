import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Amount } from "./Amount";
import { Button } from "./Button";
import { ConfirmDialog } from "./ConfirmDialog";
import { Field } from "./Field";
import { MoneyInput } from "./MoneyInput";
import { ErrorState } from "./States";
import { StatusBadge } from "./StatusBadge";

describe("Button", () => {
  it("defaults to type=button so it never submits a form by accident", () => {
    const onSubmit = vi.fn((event: Event) => {
      event.preventDefault();
    });
    render(
      <form
        onSubmit={(event) => {
          onSubmit(event.nativeEvent);
        }}
      >
        <Button>Plain</Button>
      </form>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("is disabled and marked busy while loading", () => {
    render(<Button loading>Saving...</Button>);
    const button = screen.getByRole("button", { name: "Saving..." });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveClass("btn", "btn--primary");
  });
});

describe("Field", () => {
  it("links the label, hint and error to the control", () => {
    render(
      <Field label="Amount" hint="Up to 2 decimals" error="Enter an amount">
        {(control) => <input {...control} />}
      </Field>,
    );
    const input = screen.getByLabelText("Amount");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Up to 2 decimals Enter an amount");
  });

  it("is not marked invalid without an error", () => {
    render(<Field label="Name">{(control) => <input {...control} />}</Field>);
    expect(screen.getByLabelText("Name")).not.toHaveAttribute("aria-invalid");
  });
});

describe("MoneyInput", () => {
  it("keeps the typed text and shows the currency", async () => {
    function Harness() {
      const [value, setValue] = useState("");
      return (
        <Field label="Amount (INR)">
          {(control) => (
            <MoneyInput {...control} value={value} onValueChange={setValue} currency="INR" />
          )}
        </Field>
      );
    }
    render(<Harness />);
    const input = screen.getByLabelText("Amount (INR)");
    await userEvent.setup().type(input, "1,250.50");
    expect(input).toHaveValue("1,250.50");
    expect(input).toHaveAttribute("inputmode", "decimal");
    expect(screen.getByText("INR")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Amount and StatusBadge", () => {
  it("marks credits with a sign, not only with colour", () => {
    render(
      <>
        <Amount minor={1250} currency="INR" credit />
        <Amount minor={-400} currency="INR" />
      </>,
    );
    expect(screen.getByText("+INR 12.50")).toHaveClass("amount--credit");
    expect(screen.getByText("-INR 4.00")).not.toHaveClass("amount--credit");
  });

  it("renders the status label and tone", () => {
    render(<StatusBadge status={{ label: "Under review", tone: "warning" }} />);
    expect(screen.getByText("Under review")).toHaveClass("badge--warning");
  });
});

describe("ErrorState", () => {
  it("announces the error and offers a retry", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Could not load." onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load.");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe("ConfirmDialog", () => {
  const items = [
    { label: "From", value: "Savings ••••9528" },
    { label: "Amount", value: "INR 125.50" },
  ];

  it("shows what will happen and confirms or cancels", async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        title="Review transfer"
        items={items}
        confirmLabel="Send INR 125.50"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Review transfer" });
    expect(dialog).toHaveTextContent("Savings ••••9528");
    expect(dialog).toHaveTextContent("INR 125.50");
    await user.click(screen.getByRole("button", { name: "Send INR 125.50" }));
    expect(onConfirm).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("cancels on Esc, except while the request is in flight", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <ConfirmDialog
        open
        title="Review"
        items={items}
        confirmLabel="Confirm"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(onCancel).toHaveBeenCalledOnce();

    rerender(
      <ConfirmDialog
        open
        busy
        error="insufficient funds"
        title="Review"
        items={items}
        confirmLabel="Confirm"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Confirm" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("insufficient funds");
  });

  it("is closed when not open", () => {
    render(
      <ConfirmDialog
        open={false}
        title="Review"
        items={items}
        confirmLabel="Confirm"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
