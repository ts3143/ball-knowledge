/** An error the API can show the player: a stable `code` for the client and a plain message. */
export class ServiceError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ServiceError";
  }
}

export const notFound = (what = "Not found") => new ServiceError("not_found", what, 404);
export const badRequest = (code: string, message: string) => new ServiceError(code, message, 400);
export const conflict = (code: string, message: string) => new ServiceError(code, message, 409);
