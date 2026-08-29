import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import type {
  AwsAccount, DriveStatus, CostReport, LibraryResponse, BackupStatus, SharesResponse,
} from './types'

/* The page reads only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the page's 403/409 paths. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      iamPolicy: vi.fn(),
      drive: vi.fn(),
      driveBootstrapPreview: vi.fn(),
      driveBootstrapConfirm: vi.fn(),
      driveList: vi.fn(),
      driveDownload: vi.fn(),
      driveUpload: vi.fn(),
      driveDelete: vi.fn(),
      driveFolderCreate: vi.fn(),
      driveFolderDelete: vi.fn(),
      driveShare: vi.fn(),
      shares: vi.fn(),
      shareForget: vi.fn(),
      costs: vi.fn(),
      library: vi.fn(),
      libraryPush: vi.fn(),
      backup: vi.fn(),
      backupRun: vi.fn(),
      backupNightly: vi.fn(),
      backupRestore: vi.fn(),
    },
  }
})

/* The Cost Explorer consent nudge fetches through the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import DrivePage from './DrivePage'

const ACCOUNT: AwsAccount = {
  account: '111122223333',
  name: 'personal',
  health: 'ok',
  profiles: [
    {
      name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
      account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
    },
  ],
  summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
}

const driveExists: Extract<DriveStatus, { exists: true }> = {
  exists: true,
  bucket: 'kirocrew-drive-abc123',
  region: 'us-west-2',
  usage: {
    bytes: 3_500_000_000,
    objects: 42,
    sections: {
      library: { objects: 10, bytes: 1_000_000 },
      drive: { objects: 30, bytes: 3_000_000_000 },
      backup: { objects: 2, bytes: 499_000_000 },
    },
  },
}

const costsFresh: CostReport = {
  fresh: true, monthToDate: 12.5, projected: 30, currency: 'USD',
  byService: [{ service: 'S3', amount: 12.5 }], fetchedAt: '2026-08-24T05:00:00Z',
}

const emptyLibrary: LibraryResponse = { artifacts: [] }
const emptyBackup: BackupStatus = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const noShares: SharesResponse = { shares: [] }

function stubDrivePresent() {
  vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
  vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

beforeEach(() => {
  vi.clearAllMocks()
  // The grid/list toggle persists per section to localStorage, which outlives a
  // single test. Without this, a test that switches a section's view silently
  // changes what every LATER test in the file renders -- the table controls
  // vanish and the failure points at the wrong test.
  localStorage.clear()
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
})

/**
 * Render the drive page and, optionally, open one of its three folder rows.
 *
 * The four sections that used to stack on the account console now live here:
 * the page root shows the three folder rows plus the shares ledger, and each
 * folder opens the section the console used to render inline. Tests that assert
 * the file browser pass 'drive', library tests 'library', backup tests
 * 'backup', and the shares-ledger tests (which sit at the root) pass nothing.
 */
async function renderDrive(section?: 'drive' | 'library' | 'backup') {
  renderWithProviders(<DrivePage account={ACCOUNT} drive={driveExists} onBack={() => {}} />)
  if (section) fireEvent.click(await screen.findByTestId(`drive-section-${section}`))
}

describe('DrivePage', () => {
  it('mints a share link and shows the URL exactly once in the dialog', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockResolvedValue({
      url: 'https://example-presigned/report.pdf?sig=x',
      share: {
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-24T06:00:00Z', note: '',
      },
    })

    await renderDrive('drive')

    // Share lives in the per-row overflow menu (rows carry at most two
    // sibling controls: Download + More).
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    fireEvent.click(await screen.findByTestId('share-create'))

    const result = await screen.findByTestId('share-result')
    expect(result).toHaveTextContent('https://example-presigned/report.pdf?sig=x')
    // The URL lives only inside the dialog result — not duplicated on the page.
    expect(screen.getAllByText(/example-presigned/).length).toBe(1)
  })

  it('delete asks for confirmation restating the filename, and cancel keeps the file', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockResolvedValue({ deleted: true })

    await renderDrive('drive')

    // Delete in the overflow menu opens a confirm strip — it must NOT delete.
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    const strip = await screen.findByTestId('drive-delete-confirm')
    expect(strip).toHaveTextContent('report.pdf')
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Cancel dismisses without deleting.
    fireEvent.click(screen.getByTestId('drive-delete-cancel'))
    expect(screen.queryByTestId('drive-delete-confirm')).toBeNull()
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Confirming actually deletes.
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'report.pdf'))
  })

  it('renders the shares ledger with an expires-in countdown', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: 'for review',
      }],
    })

    await renderDrive()

    const row = await screen.findByTestId('access-row')
    expect(row).toHaveTextContent('report.pdf')
    expect(row).toHaveTextContent('for review')
    // A relative "expires …" phrase renders (not a raw ISO timestamp).
    expect(row.textContent).not.toContain('2030-01-01')
  })

  it('disables the backup row and spins while a run is in flight', async () => {
    stubDrivePresent()
    // A run that never resolves keeps the row in its busy state.
    vi.mocked(awsControlApi.backupRun).mockReturnValue(new Promise(() => {}) as ReturnType<typeof awsControlApi.backupRun>)

    await renderDrive('backup')

    const runBtn = await screen.findByTestId('backup-run-snapshot')
    fireEvent.click(runBtn)
    await waitFor(() => expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(true))
  })

  /* ── Drive: stored-usage figure, folder navigation, load-more ────────────── */

  it('lists a folder and file with a download and a load-more control', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: ['invoices'],
      nextToken: 'tok-2',
    })

    await renderDrive('drive')

    // The page header carries the real stored-usage figure (drive exists).
    const usage = await screen.findByTestId('drive-usage')
    expect(usage.textContent ?? '').toContain(fmtBytes(driveExists.usage.bytes))

    // A folder row and a file row both render.
    expect(await screen.findByTestId('drive-folder')).toHaveTextContent('invoices')
    const file = await screen.findByTestId('drive-file')
    expect(file).toHaveTextContent('report.pdf')
    // A nextToken produces a Load more control.
    expect(screen.getByTestId('drive-load-more')).toBeTruthy()
  })

  it('drills into a folder from anywhere on the row, refetching for the new path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')

    // The ROW, not the name: a folder row in a file table is expected to open
    // from any of its cells, and the artifact table's own folder row does the
    // same. Pinning the row here would fail if the handler shrank back to just
    // the name text, leaving the Kind/Size/Modified cells dead.
    fireEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'invoices', ''),
    )
  })

  it('opens the folder from its name control too, for keyboard reach', async () => {
    // The inner button is the real focusable control; the row click is a mouse
    // convenience on top of it, so both must navigate.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'invoices', ''),
    )
  })

  it('attaches the folder confirm to the folder that was clicked, not the last one', async () => {
    // The confirm restates the folder name, and that name is the only guard
    // before an irreversible recursive delete - so the strip has to sit under
    // the row it belongs to. Rendered once after the whole folder list, deleting
    // the FIRST folder put the prompt under the LAST one, visually attached to a
    // folder the reader did not pick. Needs two folders to show up at all.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices', 'receipts'],
    })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')

    const rows = screen.getAllByTestId('drive-folder')
    expect(rows).toHaveLength(2)
    fireEvent.keyDown(within(rows[0]).getByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))

    const confirm = await screen.findByTestId('drive-folder-delete-confirm')
    expect(confirm).toHaveTextContent('invoices')
    // The strip is the very next row after the folder it targets.
    expect(rows[0].nextElementSibling).toBe(confirm)
  })

  it('follows the drive query, so the header totals are not the arrival snapshot', async () => {
    // The page used to render the DriveStatus it was handed at navigation time.
    // Every mutation here invalidates the drive key, so a frozen prop meant an
    // upload or delete changed the listing while the header kept the old size
    // and object count.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })
    vi.mocked(awsControlApi.driveFolderDelete).mockResolvedValue({ deleted: true, path: 'invoices', objects: 4 })
    // The refetch that follows the invalidation reports the smaller drive.
    const shrunk = { ...driveExists, usage: { bytes: 1024, objects: 3 } }

    await renderDrive('drive')
    const usage = await screen.findByTestId('drive-usage')
    await waitFor(() => expect(usage.textContent ?? '').toContain(fmtBytes(driveExists.usage.bytes)))

    vi.mocked(awsControlApi.drive).mockResolvedValue(shrunk)
    fireEvent.keyDown(await screen.findByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))
    fireEvent.click(await screen.findByTestId('drive-folder-delete-action'))

    // The header moves to the refetched figure rather than staying on the one
    // captured when the page opened.
    await waitFor(() => expect(screen.getByTestId('drive-usage').textContent ?? '').toContain(fmtBytes(1024)))
  })

  it('reports how many objects the folder delete actually removed', async () => {
    // The count is only knowable from the RESPONSE - a figure shown before
    // consent would cost a second full recursive listing - so the page states
    // what was removed. Without this the endpoint's count had no consumer while
    // the type comment claimed the UI showed it.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })
    vi.mocked(awsControlApi.driveFolderDelete).mockResolvedValue({ deleted: true, path: 'invoices', objects: 12 })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')
    fireEvent.keyDown(await screen.findByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))
    fireEvent.click(await screen.findByTestId('drive-folder-delete-action'))

    const line = await screen.findByTestId('drive-folder-deleted')
    expect(line.textContent ?? '').toContain('12')
  })

  it('rejects a bad FOLDER name with folder wording, not the file message', async () => {
    // The shared drive_bad_name text names a file; the reader typed a folder.
    stubDrivePresent()
    await renderDrive('drive')

    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: '../escape' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))

    const err = await screen.findByTestId('drive-folder-error')
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.folder_bad_name'))
    expect(err).not.toHaveTextContent(i18nT('apps.awsControl.console.drive_bad_name'))
    // Never reached the endpoint.
    expect(awsControlApi.driveFolderCreate).not.toHaveBeenCalled()
  })

  it('deleting a folder does not also open it', async () => {
    // The delete control sits inside a row whose own click navigates, so it must
    // stop the event: opening the folder you just asked to delete would swap the
    // listing out from under the confirm strip.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')
    vi.mocked(awsControlApi.driveList).mockClear()

    fireEvent.keyDown(await screen.findByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))

    // The confirm strip is up, naming the folder...
    expect(await screen.findByTestId('drive-folder-delete-confirm')).toHaveTextContent('invoices')
    // ...and no navigation happened.
    expect(awsControlApi.driveList).not.toHaveBeenCalled()
  })

  it('keeps the breadcrumb at two controls and still reaches an ancestor', async () => {
    // AUTOSDE max-two-buttons-per-row: Root plus ONE overflow, and the folder
    // you are in is text. The earlier shape rendered a button per crumb, so
    // `a/b` put three sibling buttons in one group. The ancestors moved into the
    // overflow rather than becoming flat text, so jumping to `a` from `a/b`
    // still works -- that capability is what this pins.
    // The listing returns folder values relative to the SECTION root, not to the
    // current subpath (storage.list_section strips only the section prefix), so
    // one click on `a/b` lands at depth 2.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['a/b'],
    })
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'a/b', ''),
    )

    // The crumb group holds exactly two controls: Root and the overflow.
    const crumbs = await screen.findByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(2)
    // The current folder is rendered as text, not as a third control.
    expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('b')

    // The overflow still navigates to the ancestor `a`.
    fireEvent.click(screen.getByTestId('drive-crumb-more'))
    const menu = screen.getByTestId('drive-crumb-menu')
    expect(menu.querySelectorAll('button')).toHaveLength(1)
    fireEvent.click(menu.querySelectorAll('button')[0])
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'a', ''),
    )
  })

  it('shows no breadcrumb at the section root, and no overflow one folder deep', async () => {
    // With nothing to jump PAST, an overflow would be an empty affordance - and
    // at the section root the whole breadcrumb would only repeat the section
    // header directly above it, so it is not rendered at all.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })
    await renderDrive('drive')

    // At the section root: no breadcrumb.
    await screen.findByTestId('drive-listing')
    expect(screen.queryByTestId('drive-crumbs')).toBeNull()
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()

    // One level deep: the breadcrumb appears with its root control and the
    // folder as text, and still no overflow.
    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('invoices'),
    )
    const crumbs = screen.getByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(1)
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()
  })

  it('shows the drive empty state when the listing has no files or folders', async () => {
    stubDrivePresent()
    await renderDrive('drive')
    expect(await screen.findByTestId('drive-empty')).toBeTruthy()
  })

  /* ── Drive: download handler (opens a tab synchronously) ─────────────────── */

  it('opens a blank tab synchronously and navigates it once the presign resolves', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({
      url: 'https://example-presigned/dl?sig=y', expiresSecs: 300,
    })
    // A fake tab whose location we can inspect: the handler must set its href.
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-download'))
    // The tab was opened blank inside the click, then navigated to the presign.
    // No 'noopener' feature: with it the standard makes window.open return null,
    // so requesting it would hand back no tab to navigate at all.
    expect(openSpy).toHaveBeenCalledWith('', '_blank')
    await waitFor(() => expect(fakeTab.location.href).toBe('https://example-presigned/dl?sig=y'))
    expect(fakeTab.close).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })

  it('closes the blank tab when the download presign fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    // The presign rejects — the orphan blank tab must be closed, not left open.
    vi.mocked(awsControlApi.driveDownload).mockRejectedValue(new Error('AccessDenied'))
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-download'))
    await waitFor(() => expect(fakeTab.close).toHaveBeenCalled())
    // The tab was never navigated anywhere.
    expect((fakeTab as unknown as { location: { href: string } }).location.href).toBe('')
    // And the failure is REPORTED. Rethrowing here would surface as an
    // unhandled rejection from an onClick with no catch, which tells the user
    // nothing; the row must say the download did not start.
    expect(await screen.findByTestId('drive-download-error')).toBeTruthy()
    openSpy.mockRestore()
  })

  /* ── Drive: upload flow, including the client-side bad-name guard ────────── */

  it('rejects a bad filename client-side without ever calling upload', async () => {
    stubDrivePresent()
    await renderDrive('drive')

    const input = await screen.findByTestId('drive-file-input')
    // A leading-dot name violates KEY_SEGMENT: the guard surfaces an error and
    // must NOT call the upload api.
    const bad = new File(['x'], '.hidden', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [bad] } })

    expect(await screen.findByTestId('drive-upload-error')).toBeTruthy()
    expect(awsControlApi.driveUpload).not.toHaveBeenCalled()
  })

  it('uploads a well-named file through the api and invalidates', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveUpload).mockResolvedValue({ uploaded: true, key: 'ok.txt', bytes: 1 })

    await renderDrive('drive')

    const input = await screen.findByTestId('drive-file-input')
    const good = new File(['x'], 'ok.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [good] } })

    // The api is called with the section and the file, and no error strip shows.
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'ok.txt', good),
    )
    expect(screen.queryByTestId('drive-upload-error')).toBeNull()
  })

  /* ── Drive: delete ERROR state ───────────────────────────────────────────
   * The happy delete path is covered above; this drives the mutation into its
   * error rendering (the confirm strip must show the failure and keep itself
   * open so the owner can retry). */

  it('shows the delete-failed message and keeps the confirm strip open on error', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockRejectedValue(new Error('AccessDenied'))

    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))

    // The failure surfaces in-strip and the strip stays open (no onSuccess close).
    expect(await screen.findByTestId('drive-delete-error')).toBeTruthy()
    expect(screen.getByTestId('drive-delete-confirm')).toBeTruthy()
  })

  /* ── Library: the cloud prefix, and the picker that fills it ─────────────── */

  /**
   * THE assertion for this section's redesign.
   *
   * The Library folder used to render the LOCAL artifact list, so it showed
   * artifacts that were not in the drive at all -- every one labelled "not
   * synced" -- while the Files folder beside it sat empty, which left the two
   * folders impossible to tell apart. It now lists the bucket prefix, so a card
   * is here if and only if the object is in the cloud. The local library is only
   * a name/kind lookup for the cards.
   */
  it('lists the cloud prefix, not the local artifact library', async () => {
    stubDrivePresent()
    // One artifact IS in the cloud; a second exists locally and was never pushed.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 3, pushedAt: '2026-08-21T00:00:00Z' },
        { slug: 'draft', name: 'Draft', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    await screen.findByTestId('library-section')

    // Exactly the one cloud object -- NOT the two local artifacts.
    const cards = await screen.findAllByTestId('library-card')
    expect(cards).toHaveLength(1)
    // Named from the local lookup, keyed by the prefix folder name.
    expect(cards[0].textContent).toContain('Notes')
    // The never-pushed local artifact does not appear in the folder at all.
    expect(screen.queryByText('Draft')).toBeNull()
    // And the listing came from the library section of the bucket.
    expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'library', '', '')
  })

  it('falls back to a cloud-only card when no local artifact backs the object', async () => {
    stubDrivePresent()
    // In the cloud, but the local copy is gone (deleted locally, or pushed from
    // another machine) -- there is nothing to preview.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['orphan'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })

    await renderDrive('library')

    const card = await screen.findByTestId('library-card')
    // The slug still identifies it, and the card says why there is no preview.
    expect(card.textContent).toContain('orphan')
    expect(screen.getByText(/only/i)).toBeTruthy()
  })

  it('the empty Library folder sends the reader to the picker', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'draft', name: 'Draft', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')

    // The empty state, not a bare "this folder is empty" row.
    expect(await screen.findByTestId('library-empty')).toBeTruthy()
    // Its action opens the picker rather than leaving the reader to hunt.
    fireEvent.click(screen.getByTestId('library-empty-add'))
    expect(await screen.findByTestId('library-add-dialog')).toBeTruthy()
  })

  it('the picker filters by kind chip and pushes an artifact into the drive', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    vi.mocked(awsControlApi.libraryPush).mockResolvedValue({ pushed: true, slug: 'notes', version: 3 } as never)

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')

    // Both candidates under "all"; the chips live in the picker now.
    expect(await screen.findAllByTestId('library-tile')).toHaveLength(2)
    fireEvent.click(screen.getByTestId('library-chip-image'))
    expect(screen.getAllByTestId('library-tile')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('library-chip-markdown'))
    fireEvent.click(screen.getByTestId('library-push'))
    await waitFor(() => expect(awsControlApi.libraryPush).toHaveBeenCalledWith(ACCOUNT.account, 'notes'))
  })

  /**
   * An image is refused by the backend (its kind has no pushed-file extension),
   * and images are the bulk of a real library -- so the card must SAY so. A
   * disabled button with no reason reads as a broken page.
   */
  it('tells the reader why an image cannot be added yet, instead of only disabling', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))

    expect(await screen.findByTestId('library-not-pushable')).toBeTruthy()
    // No push control at all on an image card -- nothing to click and be refused.
    expect(screen.queryByTestId('library-push')).toBeNull()
  })

  it('the picker searches by name and reports when nothing matches', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-tile')

    fireEvent.change(await screen.findByTestId('library-add-search'), { target: { value: 'zzz' } })
    expect(screen.queryByTestId('library-tile')).toBeNull()
    expect(screen.getByTestId('library-add-none')).toBeTruthy()
  })

  /* ── Review fixes: capability parity, honest counts, no self-contradiction ── */

  /**
   * Grid mode is a way of LOOKING at a folder, not a capability tier.
   *
   * The first cut gave grid cards only a Download link, so a reader who
   * preferred tiles lost Share and Delete -- and because the choice persists per
   * section, they lost them on every future visit with nothing to tell them the
   * controls existed.
   */
  it('grid mode carries the same actions the table rows carry', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: ['invoices'],
    })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid')

    // A file tile keeps Download AND gains the overflow the list row has.
    expect(screen.getByTestId('drive-grid-download')).toBeTruthy()
    expect(screen.getByTestId('drive-grid-more')).toBeTruthy()
    // A folder tile is no longer action-less.
    expect(screen.getByTestId('drive-grid-folder-more')).toBeTruthy()
  })

  /** A Delete offered in grid mode must actually be able to complete. */
  it('grid mode renders its own delete confirmation', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockResolvedValue({ deleted: true } as never)

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid')

    // The confirm strips live inside the TABLE, so grid mode needs its own or the
    // action sets state and nothing ever renders.
    // Enter on the trigger, matching how the row-overflow tests above open it:
    // the menu is a portaled Radix dropdown, which a bare click does not open.
    fireEvent.keyDown(screen.getByTestId('drive-grid-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-grid-delete'))
    expect(await screen.findByTestId('drive-grid-confirm')).toBeTruthy()
    fireEvent.click(screen.getByTestId('drive-grid-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'a.txt'))
  })

  /**
   * The empty state's primary button counts what can be added, not what exists.
   *
   * Images cannot be pushed and are the bulk of a real library, so counting them
   * made the first-run CTA promise work the picker then refuses.
   */
  it('the ready-to-add count excludes artifacts that cannot be added', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic2', name: 'Pic2', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    const cta = await screen.findByTestId('library-empty-add')
    // One addable markdown, not three artifacts.
    expect(cta.textContent).toContain('1')
    expect(cta.textContent).not.toContain('3')
  })

  /** "In the cloud only" and "In the drive" on one card are two answers to one question. */
  it('a cloud-only card does not also claim to be in the drive', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['orphan'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })

    await renderDrive('library')
    const card = await screen.findByTestId('library-card')

    expect(card.textContent).toMatch(/only/i)
    // The footer that would have said "In the drive" is suppressed, and the slug
    // is not printed twice when it is already serving as the name.
    expect(card.textContent).not.toMatch(/In the drive/i)
    expect(card.textContent!.match(/orphan/g)).toHaveLength(1)
  })

  it('the picker closes on Escape, not only on its X', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    expect(await screen.findByTestId('library-add-dialog')).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('library-add-dialog')).toBeNull())
  })

  /** "Load more" must ADD to what is on screen, not replace it. */
  it('load more appends the next page instead of swapping it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({ files: [], folders: ['first'], nextToken: 'tok' })
      .mockResolvedValueOnce({ files: [], folders: ['second'] })

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('library-load-more'))
    // Both pages are on screen -- the first did not vanish.
    await waitFor(() => expect(screen.getAllByTestId('library-card')).toHaveLength(2))
    const text = screen.getByTestId('library-grid').textContent!
    expect(text).toContain('first')
    expect(text).toContain('second')
  })

  /**
   * Every kind the backend can hand us must render a label, not a blank badge.
   *
   * `list_pushable` returns `artifact.kind` VERBATIM from the store's
   * ALLOWED_KINDS, with no filtering, and `_KIND_EXT` makes svg and text
   * pushable too -- so a union that listed only six kinds did not make the other
   * two unreachable, it just stopped the compiler from noticing their badge
   * resolved to `undefined`. A QA capture caught an SVG artifact rendering an
   * empty badge in the Library list. This is the ratchet: the list below is the
   * backend's set, so adding a kind there without a label here fails HERE.
   */
  it('renders a label for every artifact kind the backend can return', async () => {
    const BACKEND_KINDS = ['widget', 'html', 'markdown', 'svg', 'json', 'text', 'image', 'webapp'] as const
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: BACKEND_KINDS.map((kind, i) => ({
        slug: `a${i}`, name: `A${i}`, kind, version: 1,
        updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null,
      })),
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')

    // One chip per kind, and not one of them is blank.
    for (const kind of BACKEND_KINDS) {
      const chip = screen.getByTestId(`library-chip-${kind}`)
      // The chip carries a label plus its count; strip the digits and require
      // something legible is left.
      expect(chip.textContent!.replace(/\d+/g, '').trim(), `${kind} chip label`).not.toBe('')
    }
    // ...and every card's kind badge resolved too.
    const tiles = await screen.findAllByTestId('library-tile')
    expect(tiles).toHaveLength(BACKEND_KINDS.length)
  })

  it('remembers the view mode per section, so Files opens as a table', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })

    await renderDrive('drive')

    // Files holds arbitrary uploads with comparable columns, so it opens listed.
    expect(await screen.findByTestId('drive-listing')).toBeTruthy()
    expect(screen.queryByTestId('drive-grid')).toBeNull()
    // ...and the toggle switches it to tiles. The control is the shared
    // SegmentedControl (the same one the Artifacts gallery uses for grid/table),
    // so the segment is addressed by its accessible name rather than a testid of
    // its own.
    fireEvent.click(screen.getByTitle('Grid view'))
    expect(await screen.findByTestId('drive-grid')).toBeTruthy()
  })

  /* ── Share: error branch, and cancel via the close button ────────────────── */

  it('leaves the share dialog on its form when share creation fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockRejectedValue(new Error('AccessDenied'))

    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    // Pick a non-default expiry and type a note before creating.
    fireEvent.click(await screen.findByTestId('share-expiry-7d'))
    fireEvent.change(screen.getByTestId('share-note'), { target: { value: 'quarterly' } })
    fireEvent.click(screen.getByTestId('share-create'))

    // The api was called with the chosen expiry seconds + note; on failure the
    // dialog keeps its form (no result panel) so the owner can retry.
    await waitFor(() =>
      expect(awsControlApi.driveShare).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'report.pdf', 604800, 'quarterly'),
    )
    expect(screen.queryByTestId('share-result')).toBeNull()
    // The close button dismisses the dialog entirely.
    fireEvent.click(screen.getByTestId('share-close'))
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
  })

  /* ── Backup: run success, nightly toggle, stored-archive disclosure, restore ─ */

  it('runs a backup, toggles nightly, and both invalidate through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false,
      runs: { snapshot: { key: 'snap-1', bytes: 1024, at: '2026-08-24T00:00:00Z' } },
      remote: { snapshot: [], sessions: [] },
    })
    vi.mocked(awsControlApi.backupRun).mockResolvedValue({
      ran: true, kind: 'snapshot', run: { key: 'snap-2', bytes: 2048, at: '2026-08-25T00:00:00Z' },
    })
    vi.mocked(awsControlApi.backupNightly).mockResolvedValue({ nightly: true } as never)

    await renderDrive('backup')

    // The snapshot row shows its last-run line, then Run now calls the api.
    await screen.findByTestId('backup-row-snapshot')
    fireEvent.click(screen.getByTestId('backup-run-snapshot'))
    await waitFor(() => expect(awsControlApi.backupRun).toHaveBeenCalledWith(ACCOUNT.account, 'snapshot'))

    // The sessions row carries its extra scope caveat.
    expect(screen.getByTestId('backup-sessions-scope')).toBeTruthy()

    // Flipping the nightly toggle calls the api with the new value.
    const toggle = within(screen.getByTestId('backup-nightly')).getByRole('switch')
    fireEvent.click(toggle)
    await waitFor(() => expect(awsControlApi.backupNightly).toHaveBeenCalledWith(ACCOUNT.account, true))
  })

  it('discloses the stored-backups archive and restores a file, showing the staged path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: true,
      runs: {},
      remote: {
        snapshot: [{ key: 'backup/snapshot/2026-08-24.tar', size: 4096, modified: '2026-08-24T00:00:00Z' }],
        sessions: [],
      },
    })
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/home/u/.kiro/restore/2026-08-24', bytes: 4096,
    })

    await renderDrive('backup')

    // The archive is behind a disclosure; before opening, no rows are shown.
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    expect(within(archive).getByTestId('backup-archive-row')).toHaveTextContent('2026-08-24.tar')
    // The write-only-tier restore caveat renders alongside.
    expect(screen.getByTestId('backup-restore-caveat')).toBeTruthy()

    // Restore stages the archive locally and echoes the landed path.
    fireEvent.click(within(archive).getByTestId('backup-restore'))
    await waitFor(() => expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT.account, 'backup/snapshot/2026-08-24.tar'))
    expect(await screen.findByTestId('backup-restored')).toHaveTextContent('/home/u/.kiro/restore/2026-08-24')
  })

  it('shows the backup remote-error note when the archive could not be read', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false, runs: {}, remote: null, remoteError: 'AccessDenied',
    })

    await renderDrive('backup')

    // A null remote with a reason renders the muted error note and NO disclosure.
    expect(await screen.findByTestId('backup-remote-error')).toBeTruthy()
    expect(screen.queryByTestId('backup-remote-toggle')).toBeNull()
  })

  /* ── Access: forget a share ──────────────────────────────────────────────── */

  it('forgets a share through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT.account, section: 'library', key: 'w/notes',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
      }],
    })
    vi.mocked(awsControlApi.shareForget).mockResolvedValue({ forgotten: true } as never)

    await renderDrive()

    fireEvent.click(await screen.findByTestId('access-forget'))
    await waitFor(() => expect(awsControlApi.shareForget).toHaveBeenCalledWith('s1'))
  })

  /* ── CLI drawer disclosure ───────────────────────────────────────────────── */

  it('reveals a copyable CLI line in the library section drawer', async () => {
    stubDrivePresent()
    await renderDrive('library')

    // The drawer is collapsed by default; opening it shows the aws s3 ls line
    // scoped to the artifacts/ prefix of the account's bucket.
    const drawers = await screen.findAllByTestId('cli-drawer-toggle')
    fireEvent.click(drawers[0])
    const body = await screen.findByTestId('cli-drawer-body')
    expect(body).toHaveTextContent('aws s3 ls s3://kirocrew-drive-abc123/artifacts/')
  })
})

describe('download tab: the noopener trap', () => {
  it('opens the synchronous tab WITHOUT noopener and nulls opener instead', async () => {
    // Per the HTML standard a window.open carrying 'noopener' returns NULL, so
    // requesting it here makes the synchronous handle unusable and the whole
    // preserve-user-activation fix a no-op. The previous test suite could not
    // see that: it MOCKED window.open into returning a tab, so the mock was
    // greener than the browser. This asserts the CALL, which is the only part a
    // mock cannot lie about, plus the isolation that replaces the feature.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({
      url: 'https://signed.example/report.pdf',
      expiresSecs: 900,
    })
    const fakeTab = { location: { href: '' }, close: vi.fn(), opener: {} } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    renderWithProviders(<DrivePage account={ACCOUNT} drive={driveExists} onBack={() => {}} />)
    fireEvent.click(await screen.findByTestId('drive-section-drive'))
    fireEvent.click(await screen.findByTestId('drive-download'))

    const firstArgs = openSpy.mock.calls[0]
    expect(firstArgs[0]).toBe('')
    expect(String(firstArgs[2] ?? '')).not.toContain('noopener')
    // The isolation the feature would have given is applied to the handle.
    expect((fakeTab as unknown as { opener: unknown }).opener).toBeNull()
    await waitFor(() =>
      expect((fakeTab as unknown as { location: { href: string } }).location.href).toBe(
        'https://signed.example/report.pdf',
      ),
    )
    openSpy.mockRestore()
  })
})
