/**
 * Sequential execution queue to serialize mutating actions arriving concurrently.
 */
export class SerializedQueue {
  private tail: Promise<any> = Promise.resolve();

  enqueue<T>(task: () => Promise<T>): Promise<T> {
    const next = this.tail.then(() => task(), () => task());
    this.tail = next.catch(() => {});
    return next;
  }
}
