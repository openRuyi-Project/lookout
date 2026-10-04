import type {DetailDocument, ListingDocument} from './lib/document';

declare global {
  namespace App {
    interface Locals {
      listing?: {data: ListingDocument | null; status: number};
      detail?: {data: DetailDocument | null; status: number};
    }
  }
}
