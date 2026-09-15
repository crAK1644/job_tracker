import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Archive,
  ArrowUpRight,
  Briefcase,
  Building2,
  Check,
  ChevronRight,
  CircleAlert,
  Clipboard,
  ExternalLink,
  FileText,
  LoaderCircle,
  MapPin,
  MoreHorizontal,
  RefreshCw,
  Search,
  Target,
  ThumbsDown,
  ThumbsUp,
  Upload,
} from 'lucide-react'
import { toast } from 'sonner'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

import {
  filterJobs,
  formatPostedAt,
  formatRunTime,
  statusLabels,
  workplaceLabels,
  type Dashboard,
  type Job,
  type JobStatus,
  type Scope,
  type SortOrder,
} from './lib/dashboard'

const editableStatuses: JobStatus[] = ['new', 'interested', 'applied', 'rejected', 'ignored']

function App() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [scope, setScope] = useState<Scope>('active')
  const [query, setQuery] = useState('')
  const [workplace, setWorkplace] = useState('all')
  const [source, setSource] = useState('all')
  const [status, setStatus] = useState('all')
  const [sort, setSort] = useState<SortOrder>('score')
  const [selectedUid, setSelectedUid] = useState<string | null>(null)
  const [savingUid, setSavingUid] = useState<string | null>(null)
  const [uploadingCv, setUploadingCv] = useState(false)
  const cvInputRef = useRef<HTMLInputElement>(null)

  async function loadDashboard() {
    setLoading(true)
    setLoadError(null)
    try {
      const response = await fetch('/api/dashboard')
      if (!response.ok) throw new Error('Panel verisi alınamadı.')
      setDashboard(await response.json() as Dashboard)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Panel verisi alınamadı.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadDashboard()
  }, [])

  const jobs = useMemo(
    () => filterJobs(dashboard?.jobs ?? [], { scope, query, workplace, source, status, sort }),
    [dashboard, scope, query, workplace, source, status, sort],
  )
  const sources = useMemo(
    () => [...new Set((dashboard?.jobs ?? []).map((job) => job.source).filter(Boolean))].sort(),
    [dashboard],
  )
  const maxScore = useMemo(
    () => Math.max(...(dashboard?.jobs ?? []).map((job) => job.score), 1),
    [dashboard],
  )
  const selectedJob = dashboard?.jobs.find((job) => job.uid === selectedUid) ?? null

  async function changeStatus(job: Job, nextStatus: JobStatus) {
    if (job.status === nextStatus || job.status === 'closed') return
    const previous = dashboard
    setSavingUid(job.uid)
    setDashboard((current) => current && {
      ...current,
      jobs: current.jobs.map((item) => item.uid === job.uid ? { ...item, status: nextStatus } : item),
      counts: {
        ...current.counts,
        [job.status]: Math.max(0, current.counts[job.status] - 1),
        [nextStatus]: current.counts[nextStatus] + 1,
      },
    })
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(job.uid)}/status`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: nextStatus }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { detail?: string } | null
        throw new Error(body?.detail ?? 'Durum kaydedilemedi.')
      }
      toast.success('Durum güncellendi', {
        description: `${job.title} artık “${statusLabels[nextStatus]}” listesinde.`,
      })
    } catch (error) {
      setDashboard(previous)
      toast.error('Değişiklik kaydedilemedi', {
        description: error instanceof Error ? error.message : 'Bağlantıyı kontrol edip yeniden deneyin.',
      })
    } finally {
      setSavingUid(null)
    }
  }

  async function copyCvCommand(job: Job) {
    if (!/^[0-9a-f]{16}$/i.test(job.uid)) {
      toast.error('CV komutu oluşturulamadı', { description: 'İlan kimliği beklenen biçimde değil.' })
      return
    }
    try {
      await navigator.clipboard.writeText(`uv run python run.py cv build ${job.uid}`)
      toast.success('CV komutu panoya kopyalandı')
    } catch {
      toast.error('Panoya kopyalanamadı', { description: 'Tarayıcı izinlerini kontrol edin.' })
    }
  }

  async function uploadCv(file: File) {
    setUploadingCv(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const response = await fetch('/api/cv', { method: 'POST', body })
      const data = await response.json().catch(() => null) as { detail?: string; skills?: string[]; seniority?: string } | null
      if (!response.ok) throw new Error(data?.detail ?? 'CV işlenemedi.')
      toast.success('CV işlendi, ilanlar becerilerine göre yeniden sıralandı', {
        description: `${data?.skills?.length ?? 0} beceri algılandı${data?.seniority ? ` · ${data.seniority}` : ''}.`,
      })
      await loadDashboard()
    } catch (error) {
      toast.error('CV yüklenemedi', {
        description: error instanceof Error ? error.message : 'Dosyayı kontrol edip yeniden deneyin.',
      })
    } finally {
      setUploadingCv(false)
    }
  }

  const activeCount = (dashboard?.counts.new ?? 0) + (dashboard?.counts.interested ?? 0) + (dashboard?.counts.applied ?? 0)
  const archiveCount = (dashboard?.counts.rejected ?? 0) + (dashboard?.counts.ignored ?? 0) + (dashboard?.counts.closed ?? 0)

  return (
    <main className="min-h-screen pb-12">
      <div className="mx-auto max-w-7xl px-4 pt-6 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-5 border-b border-border pb-6 md:flex-row md:items-end md:justify-between">
          <div className="max-w-2xl">
            <div className="mb-3 flex items-center gap-2 text-sm text-primary">
              <Target className="size-4" aria-hidden="true" />
              <span>İstanbul ve uzaktan DS · ML · AI fırsatları</span>
            </div>
            <h1 className="text-3xl font-semibold tracking-[-0.045em] text-foreground sm:text-4xl">İş radarı</h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              Uygun ilanları tara, önceliklendir ve başvuru sürecini tek bir çalışma yüzeyinden yönet.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>{dashboard?.runId ? `Son tarama: ${formatRunTime(dashboard.runId)}` : 'Henüz tarama yok'}</span>
            {dashboard?.cv.active ? (
              <Badge variant="outline" className="gap-1 border-primary/25 bg-primary/5 text-primary">
                <FileText className="size-3.5" /> CV yüklü · {dashboard.cv.skillCount} beceri
              </Badge>
            ) : null}
            <input
              ref={cvInputRef}
              type="file"
              accept=".pdf,.txt,.md"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void uploadCv(file)
              }}
            />
            <Button variant="outline" size="sm" onClick={() => cvInputRef.current?.click()} disabled={uploadingCv}>
              {uploadingCv ? <LoaderCircle className="animate-spin" /> : <Upload />}
              {dashboard?.cv.active ? 'CV değiştir' : 'CV yükle'}
            </Button>
            <Button variant="outline" size="sm" onClick={() => void loadDashboard()} disabled={loading}>
              <RefreshCw className={loading ? 'animate-spin' : ''} />
              Yenile
            </Button>
          </div>
        </header>

        {loading && !dashboard ? <DashboardSkeleton /> : null}
        {loadError ? <LoadError message={loadError} onRetry={() => void loadDashboard()} /> : null}

        {dashboard ? (
          <>
            <section className="grid gap-3 py-6 sm:grid-cols-2 lg:grid-cols-4" aria-label="İlan özeti">
              <MetricCard label="Aktif ilan" value={activeCount} icon={<Briefcase />} />
              <MetricCard label="Yeni fırsat" value={dashboard.counts.new ?? 0} icon={<Target />} signal="fresh" />
              <MetricCard label="İlgileniyorum" value={dashboard.counts.interested ?? 0} icon={<Building2 />} />
              <MetricCard label="Başvurdum" value={dashboard.counts.applied ?? 0} icon={<Check />} />
            </section>

            {dashboard.errors.length > 0 ? (
              <Alert className="mb-5 border-amber-300/20 bg-amber-300/8 text-amber-100">
                <CircleAlert className="size-4" />
                <AlertTitle>Bu taramada eksik kaynak var</AlertTitle>
                <AlertDescription className="mt-1 text-amber-100/75">
                  {dashboard.errors.join(' · ')}
                </AlertDescription>
              </Alert>
            ) : null}

            <section className="sticky top-0 z-20 mb-5 rounded-xl border border-border/90 bg-card/90 shadow-2xl shadow-black/10 backdrop-blur" aria-label="İlan filtreleri">
              <div className="flex flex-col gap-4 p-3 sm:p-4">
                <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-center">
                  <Tabs value={scope} onValueChange={(value) => { setScope(value as Scope); setStatus('all') }}>
                    <TabsList className="h-9 bg-muted/70">
                      <TabsTrigger value="active" className="gap-1.5 px-3">Aktif <span className="text-muted-foreground">{activeCount}</span></TabsTrigger>
                      <TabsTrigger value="archive" className="gap-1.5 px-3"><Archive className="size-3.5" /> Arşiv <span className="text-muted-foreground">{archiveCount}</span></TabsTrigger>
                    </TabsList>
                  </Tabs>
                  <div className="flex flex-wrap items-center gap-2">
                    {dashboard.links.linkedin.slice(0, 2).map((link) => (
                      <Button key={link.url} variant="ghost" size="sm" asChild className="text-muted-foreground hover:text-foreground">
                        <a href={link.url} target="_blank" rel="noreferrer">LinkedIn: {link.label}<ArrowUpRight /></a>
                      </Button>
                    ))}
                  </div>
                </div>

                <div className="grid gap-2 lg:grid-cols-[minmax(15rem,1fr)_repeat(4,minmax(9rem,auto))]">
                  <label className="relative block">
                    <span className="sr-only">İlanlarda ara</span>
                    <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                    <Input value={query} onChange={(event) => setQuery(event.target.value)} className="h-9 bg-background/50 pl-8" placeholder="Şirket, rol veya beceri ara" />
                  </label>
                  <FilterSelect value={workplace} onValueChange={setWorkplace} placeholder="Çalışma biçimi" items={[
                    ['all', 'Tüm çalışma biçimleri'], ['remote', 'Uzaktan'], ['hybrid', 'Hibrit'], ['onsite', 'Ofis'], ['unknown', 'Belirsiz'],
                  ]} />
                  <FilterSelect value={source} onValueChange={setSource} placeholder="Kaynak" items={[['all', 'Tüm kaynaklar'], ...sources.map((item) => [item, item])]} />
                  <FilterSelect value={status} onValueChange={setStatus} placeholder="Durum" items={[
                    ['all', 'Tüm durumlar'],
                    ...(scope === 'active' ? ['new', 'interested', 'applied'] : ['rejected', 'ignored', 'closed']).map((item) => [item, statusLabels[item as JobStatus]]),
                  ]} />
                  <FilterSelect value={sort} onValueChange={(value) => setSort(value as SortOrder)} placeholder="Sıralama" items={[
                    ['score', 'Uyum puanı'], ['newest', 'En yeni'], ['company', 'Şirket adı'],
                  ]} />
                </div>
              </div>
            </section>

            <div className="mb-3 flex items-center justify-between text-sm text-muted-foreground">
              <span>{jobs.length} ilan gösteriliyor</span>
              <span>{scope === 'active' ? 'Başvuru için açık fırsatlar' : 'Geçmiş kararlar ve kapanan ilanlar'}</span>
            </div>

            {jobs.length > 0 ? (
              <section className="space-y-3" aria-live="polite" aria-label="İlan listesi">
                {jobs.map((job) => (
                  <JobCard
                    key={job.uid}
                    job={job}
                    maxScore={maxScore}
                    isNew={job.firstSeen === dashboard.runId}
                    saving={savingUid === job.uid}
                    onOpen={() => setSelectedUid(job.uid)}
                    onChangeStatus={changeStatus}
                    onCopyCv={copyCvCommand}
                  />
                ))}
              </section>
            ) : (
              <EmptyState scope={scope} hasFilters={Boolean(query || workplace !== 'all' || source !== 'all' || status !== 'all')} onClear={() => { setQuery(''); setWorkplace('all'); setSource('all'); setStatus('all') }} />
            )}
          </>
        ) : null}
      </div>

      <JobDetails job={selectedJob} open={Boolean(selectedJob)} onOpenChange={(open) => !open && setSelectedUid(null)} maxScore={maxScore} saving={selectedJob ? savingUid === selectedJob.uid : false} onChangeStatus={changeStatus} onCopyCv={copyCvCommand} />
    </main>
  )
}

function MetricCard({ label, value, icon, signal }: { label: string; value: number; icon: React.ReactNode; signal?: 'fresh' }) {
  return <Card className="overflow-hidden border-border/80 bg-card/70 shadow-none"><CardContent className="flex items-start justify-between p-4"><div><p className="text-sm text-muted-foreground">{label}</p><p className="mt-2 text-3xl font-semibold tracking-[-0.04em]">{value}</p></div><span className={signal === 'fresh' ? 'rounded-lg bg-emerald-400/10 p-2 text-emerald-300' : 'rounded-lg bg-primary/10 p-2 text-primary'}>{icon}</span></CardContent></Card>
}

function FilterSelect({ value, onValueChange, placeholder, items }: { value: string; onValueChange: (value: string) => void; placeholder: string; items: string[][] }) {
  return <Select value={value} onValueChange={onValueChange}><SelectTrigger className="h-9 w-full bg-background/50 lg:w-[10.5rem]"><SelectValue placeholder={placeholder} /></SelectTrigger><SelectContent>{items.map(([itemValue, label]) => <SelectItem key={itemValue} value={itemValue}>{label}</SelectItem>)}</SelectContent></Select>
}

function JobCard({ job, maxScore, isNew, saving, onOpen, onChangeStatus, onCopyCv }: { job: Job; maxScore: number; isNew: boolean; saving: boolean; onOpen: () => void; onChangeStatus: (job: Job, status: JobStatus) => void; onCopyCv: (job: Job) => void }) {
  const score = Math.round((job.score / maxScore) * 100)
  return <Card className="group overflow-hidden border-border/80 bg-card/80 shadow-none transition-colors hover:border-primary/35"><CardContent className="grid gap-4 p-4 lg:grid-cols-[5.5rem_minmax(0,1fr)_auto] lg:items-start lg:p-5"><div className="rounded-lg border border-border/80 bg-background/45 p-3 lg:row-span-2"><div className="flex items-baseline justify-between gap-1"><span className="text-2xl font-semibold tracking-[-0.05em]">{Math.round(job.score)}</span><span className="text-xs text-muted-foreground">puan</span></div><Progress value={score} className="mt-2 h-1.5 bg-muted" /><p className="mt-2 text-xs text-muted-foreground">uyum oranı</p></div><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="font-medium text-foreground">{job.company}</span>{isNew ? <Badge className="border-0 bg-emerald-400/15 text-emerald-200 hover:bg-emerald-400/15">Yeni</Badge> : null}<StatusBadge status={job.status} /></div><button type="button" onClick={onOpen} className="mt-1 flex max-w-full items-center gap-1 text-left text-lg font-semibold tracking-[-0.025em] text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><span className="truncate">{job.title}</span><ChevronRight className="size-4 shrink-0" aria-hidden="true" /></button><div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground"><span className="inline-flex items-center gap-1"><MapPin className="size-3.5" />{job.location || 'Konum belirtilmemiş'}</span><span>{workplaceLabels[job.workplace] ?? job.workplace}</span><span>{formatPostedAt(job.postedAt)}</span><span className="truncate">{job.source}</span></div>{job.matchedSkills.length > 0 ? <div className="mt-3 flex flex-wrap gap-1.5">{job.matchedSkills.map((skill) => <Badge key={skill} variant="outline" className="border-primary/20 bg-primary/5 px-2 py-0.5 font-normal text-primary/90">{skill}</Badge>)}</div> : null}</div><JobActions job={job} saving={saving} onChangeStatus={onChangeStatus} onCopyCv={onCopyCv} /></CardContent></Card>
}

function JobActions({ job, saving, onChangeStatus, onCopyCv }: { job: Job; saving: boolean; onChangeStatus: (job: Job, status: JobStatus) => void; onCopyCv: (job: Job) => void }) {
  const otherStatuses = editableStatuses.filter((status) => status !== 'interested' && status !== 'rejected')
  return <div className="flex flex-wrap items-center gap-1.5 lg:justify-self-end">{job.status !== 'closed' ? <><Button variant={job.status === 'interested' ? 'default' : 'secondary'} size="sm" disabled={saving || job.status === 'interested'} aria-pressed={job.status === 'interested'} onClick={() => onChangeStatus(job, 'interested')}><ThumbsUp />İlgileniyorum</Button><Button variant="outline" size="sm" disabled={saving || job.status === 'rejected'} aria-pressed={job.status === 'rejected'} className={job.status === 'rejected' ? 'border-rose-300/30 bg-rose-300/10 text-rose-100 hover:bg-rose-300/10' : 'text-muted-foreground hover:text-rose-100'} onClick={() => onChangeStatus(job, 'rejected')}><ThumbsDown />İlgilenmiyorum</Button></> : null}{job.url ? <Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon-sm" asChild><a href={job.url} target="_blank" rel="noreferrer" aria-label="İlanı aç"><ExternalLink /></a></Button></TooltipTrigger><TooltipContent>İlanı aç</TooltipContent></Tooltip> : null}<Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon-sm" onClick={() => void onCopyCv(job)} aria-label="CV komutunu kopyala"><Clipboard /></Button></TooltipTrigger><TooltipContent>CV komutunu kopyala</TooltipContent></Tooltip>{job.status !== 'closed' ? <DropdownMenu><DropdownMenuTrigger asChild><Button variant="outline" size="icon-sm" disabled={saving} aria-label="Diğer durumlar">{saving ? <LoaderCircle className="animate-spin" /> : <MoreHorizontal />}</Button></DropdownMenuTrigger><DropdownMenuContent align="end" className="w-48"><DropdownMenuLabel>Diğer durumlar</DropdownMenuLabel><DropdownMenuSeparator />{otherStatuses.map((next) => <DropdownMenuItem key={next} disabled={job.status === next} onSelect={() => onChangeStatus(job, next)}>{statusLabels[next]}{job.status === next ? <Check className="ml-auto" /> : null}</DropdownMenuItem>)}</DropdownMenuContent></DropdownMenu> : <Badge variant="outline" className="text-muted-foreground">Kapandı</Badge>}</div>
}

function StatusBadge({ status }: { status: JobStatus }) {
  const colors: Record<JobStatus, string> = { new: 'border-primary/20 bg-primary/10 text-primary', interested: 'border-violet-300/20 bg-violet-300/10 text-violet-200', applied: 'border-cyan-300/20 bg-cyan-300/10 text-cyan-200', rejected: 'border-rose-300/20 bg-rose-300/10 text-rose-200', ignored: 'border-border bg-muted text-muted-foreground', closed: 'border-border bg-muted text-muted-foreground' }
  return <Badge variant="outline" className={colors[status]}>{statusLabels[status]}</Badge>
}

function JobDetails({ job, open, onOpenChange, maxScore, saving, onChangeStatus, onCopyCv }: { job: Job | null; open: boolean; onOpenChange: (open: boolean) => void; maxScore: number; saving: boolean; onChangeStatus: (job: Job, status: JobStatus) => void; onCopyCv: (job: Job) => void }) {
  if (!job) return null
  return <Sheet open={open} onOpenChange={onOpenChange}><SheetContent className="w-full overflow-y-auto border-border bg-card p-0 sm:max-w-xl"><div className="p-6 sm:p-8"><SheetHeader className="text-left"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium text-primary">{job.company}</span><StatusBadge status={job.status} /></div><SheetTitle className="mt-2 text-2xl leading-tight tracking-[-0.04em]">{job.title}</SheetTitle></SheetHeader><div className="mt-5 flex flex-wrap gap-2"><Badge variant="outline">{job.location || 'Konum belirtilmemiş'}</Badge><Badge variant="outline">{workplaceLabels[job.workplace] ?? job.workplace}</Badge><Badge variant="outline">{Math.round(job.score)} puan</Badge></div><Progress value={Math.round((job.score / maxScore) * 100)} className="mt-3 h-1.5" /><div className="mt-6 flex flex-wrap gap-2">{job.url ? <Button asChild><a href={job.url} target="_blank" rel="noreferrer">İlanı aç <ExternalLink /></a></Button> : null}<Button variant="outline" onClick={() => void onCopyCv(job)}><Clipboard />CV komutunu kopyala</Button>{job.status !== 'closed' ? <DropdownMenu><DropdownMenuTrigger asChild><Button variant="secondary" disabled={saving}>{saving ? <LoaderCircle className="animate-spin" /> : null}Durumu değiştir</Button></DropdownMenuTrigger><DropdownMenuContent align="end">{editableStatuses.map((next) => <DropdownMenuItem key={next} disabled={job.status === next} onSelect={() => onChangeStatus(job, next)}>{statusLabels[next]}</DropdownMenuItem>)}</DropdownMenuContent></DropdownMenu> : null}</div>{job.matchedSkills.length > 0 ? <section className="mt-8"><h2 className="text-sm font-medium text-foreground">Eşleşen beceriler</h2><div className="mt-3 flex flex-wrap gap-1.5">{job.matchedSkills.map((skill) => <Badge key={skill} variant="outline" className="border-primary/20 bg-primary/5 text-primary">{skill}</Badge>)}</div></section> : null}<section className="mt-8"><h2 className="text-sm font-medium text-foreground">İlan açıklaması</h2><p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-muted-foreground">{job.description || 'İlan kaynağı açıklama paylaşmadı.'}</p></section><section className="mt-8 border-t border-border pt-5 text-sm text-muted-foreground"><dl className="grid grid-cols-2 gap-x-4 gap-y-3"><div><dt>Kaynak</dt><dd className="mt-0.5 text-foreground">{job.source || '—'}</dd></div><div><dt>Yayın tarihi</dt><dd className="mt-0.5 text-foreground">{formatPostedAt(job.postedAt)}</dd></div><div><dt>Son görüldü</dt><dd className="mt-0.5 text-foreground">{formatRunTime(job.lastSeen)}</dd></div><div><dt>İlan kimliği</dt><dd className="mt-0.5 break-all font-mono text-xs text-foreground">{job.uid}</dd></div></dl></section></div></SheetContent></Sheet>
}

function DashboardSkeleton() { return <div className="space-y-5 py-6"><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-28 rounded-xl" />)}</div>{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-40 rounded-xl" />)}</div> }

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) { return <Alert variant="destructive" className="mt-6"><CircleAlert /><AlertTitle>Panel yüklenemedi</AlertTitle><AlertDescription className="mt-2 flex flex-wrap items-center gap-3"><span>{message}</span><Button variant="outline" size="sm" onClick={onRetry}>Tekrar dene</Button></AlertDescription></Alert> }

function EmptyState({ scope, hasFilters, onClear }: { scope: Scope; hasFilters: boolean; onClear: () => void }) { return <Card className="border-dashed border-border bg-card/50"><CardContent className="flex flex-col items-center px-6 py-16 text-center"><div className="rounded-full bg-muted p-3 text-muted-foreground">{scope === 'active' ? <Search /> : <Archive />}</div><h2 className="mt-4 text-lg font-semibold">{hasFilters ? 'Bu filtrelerle eşleşen ilan yok' : scope === 'active' ? 'Şu an aktif ilan yok' : 'Arşiv henüz boş'}</h2><p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">{hasFilters ? 'Aramayı genişletin veya filtreleri temizleyin.' : scope === 'active' ? '`uv run python run.py fetch` komutu yeni ilanları veritabanına ekler.' : 'Bir ilanı reddettiğinizde veya yok saydığınızda burada görünecek.'}</p>{hasFilters ? <Button variant="outline" className="mt-5" onClick={onClear}>Filtreleri temizle</Button> : null}</CardContent></Card> }

export default App
