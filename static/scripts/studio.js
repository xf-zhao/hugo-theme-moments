(() => {
    'use strict';
    const composer = document.querySelector('#studio-composer');
    if (!composer) return;
    const form = document.querySelector('#studio-post-form');
    const connection = document.querySelector('#studio-connection');
    const message = document.querySelector('#studio-post-message');
    const photoList = document.querySelector('#studio-photos');
    const newButton = document.querySelector('#studio-new');
    const toast = document.querySelector('#studio-toast');
    const rows = [...document.querySelectorAll('.moment-row[data-moment-id]')];
    const moments = new Map();
    let session;
    let editing = null;
    let retainedPhotos = [];
    let uploads = [];
    let dirty = false;
    let submitting = false;
    let toastTimer;

    function say(text) {
        toast.textContent = text;
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
    }

    async function request(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (options.method && options.method !== 'GET') headers.set('X-Moments-Token', session.token);
        if (options.body && !(options.body instanceof FormData)) {
            headers.set('Content-Type', 'application/json');
            options.body = JSON.stringify(options.body);
        }
        const response = await fetch(path, { ...options, headers });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Your change could not be saved');
        return result;
    }

    function localDate() {
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
        }).formatToParts(new Date());
        const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
        return `${value.year}-${value.month}-${value.day}T${value.hour}:${value.minute}`;
    }

    function saveLocation() {
        const date = form.elements.date.value.split('T')[0].replaceAll('-', '/');
        document.querySelector('#studio-save-location').textContent = `Saved in Moments/${date}/`;
    }

    function photoURL(picture) {
        if (/^(https?:\/\/|\/\/|\/)/.test(picture)) return picture;
        return new URL(picture, new URL(editing.url, location.origin)).href;
    }

    function renderPhotos() {
        photoList.replaceChildren();
        const pictures = [
            ...retainedPhotos.map(src => ({ src: photoURL(src), key: src, retained: true })),
            ...uploads.map(item => ({ src: item.url, key: item, retained: false }))
        ];
        for (const picture of pictures) {
            const tile = document.createElement('div');
            tile.className = 'studio-photo';
            const image = document.createElement('img');
            image.src = picture.src;
            image.alt = 'Attached photo';
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'studio-remove-photo';
            button.textContent = '×';
            button.setAttribute('aria-label', 'Remove photo from this moment');
            button.addEventListener('click', () => {
                if (picture.retained) retainedPhotos = retainedPhotos.filter(src => src !== picture.key);
                else {
                    URL.revokeObjectURL(picture.key.url);
                    uploads = uploads.filter(item => item !== picture.key);
                }
                dirty = true;
                renderPhotos();
            });
            tile.append(image, button);
            photoList.append(tile);
        }
    }

    function addPhotos(files) {
        for (const file of files) {
            if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif'].includes(file.type)) {
                say('Choose JPEG, PNG, GIF, WebP, or AVIF photos.');
                continue;
            }
            if (file.size > 10 * 1024 * 1024) { say('Each photo must be 10 MB or smaller.'); continue; }
            if (retainedPhotos.length + uploads.length >= 20) { say('You can attach up to 20 photos.'); break; }
            uploads.push({ file, url: URL.createObjectURL(file) });
            dirty = true;
        }
        renderPhotos();
    }

    function openComposer(moment = null) {
        for (const item of uploads) URL.revokeObjectURL(item.url);
        uploads = [];
        retainedPhotos = moment ? [...moment.pictures] : [];
        editing = moment;
        form.reset();
        message.textContent = '';
        form.querySelector('details').open = false;
        document.querySelector('#studio-composer-title').textContent = moment ? (moment.draft ? 'Edit draft' : 'Edit moment') : 'New moment';
        document.querySelector('#studio-publish').textContent = moment && !moment.draft ? 'Save changes' : 'Post';
        if (moment) {
            for (const key of ['body', 'note', 'link', 'link_text', 'top']) form.elements[key].value = moment[key] || '';
            form.elements.tags.value = moment.tags.join(', ');
        }
        form.elements.date.value = moment ? moment.date.slice(0, 16) : localDate();
        saveLocation();
        renderPhotos();
        dirty = false;
        composer.showModal();
        form.elements.body.focus();
    }

    function closeComposer() {
        if (submitting) return;
        if (dirty && !window.confirm('Discard the unsaved changes?')) return;
        dirty = false;
        composer.close();
    }

    function socialForRow(row, social) {
        const like = row.querySelector('.studio-like');
        if (!like) return;
        like.setAttribute('aria-pressed', String(Boolean(social.liked)));
        like.firstElementChild.textContent = social.liked ? '♥' : '♡';
        row.querySelector('.studio-like-label').textContent = social.liked ? 'Liked · 1' : 'Like';
        row.querySelector('.studio-comment-count').textContent = social.comments.length;
        const list = row.querySelector('.studio-comment-list');
        list.replaceChildren();
        for (const comment of social.comments) {
            const item = document.createElement('article');
            item.className = 'studio-comment';
            if (comment.parent_id) item.classList.add('studio-comment-reply');
            const heading = document.createElement('div');
            heading.className = 'studio-comment-heading';
            const name = document.createElement('strong');
            name.textContent = comment.author;
            const when = document.createElement('time');
            when.dateTime = comment.date;
            when.textContent = new Date(comment.date).toLocaleString('en-GB', {
                timeZone: 'Asia/Shanghai', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
            }) + (comment.edited_at ? ' · edited' : '');
            heading.append(name, when);
            const content = document.createElement('p');
            content.textContent = comment.text;
            const actions = document.createElement('div');
            actions.className = 'studio-comment-actions';
            for (const action of ['Reply', 'Edit']) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'studio-action';
                button.textContent = action;
                button.addEventListener('click', () => {
                    const commentForm = row.querySelector('.studio-comment-form');
                    commentForm.dataset.parentId = action === 'Reply' ? comment.id : '';
                    commentForm.dataset.editId = action === 'Edit' ? comment.id : '';
                    commentForm.elements.text.value = action === 'Edit' ? comment.text : '';
                    commentForm.elements.author.value = action === 'Edit' ? comment.author : session.author;
                    commentForm.elements.author.readOnly = action === 'Edit';
                    commentForm.querySelector('[type=submit]').textContent = action === 'Edit' ? 'Save comment' : 'Reply';
                    const status = commentForm.querySelector('.studio-reply-status');
                    status.replaceChildren(document.createTextNode(action === 'Edit' ? 'Editing your comment ' : `Replying to ${comment.author} `));
                    const cancel = document.createElement('button');
                    cancel.type = 'button';
                    cancel.className = 'studio-action';
                    cancel.textContent = 'Cancel';
                    cancel.addEventListener('click', () => resetCommentForm(commentForm));
                    status.append(cancel);
                    status.hidden = false;
                    commentForm.elements.text.focus();
                });
                actions.append(button);
            }
            item.append(heading, content, actions);
            list.append(item);
        }
        if (!social.comments.length) {
            const empty = document.createElement('p');
            empty.className = 'studio-muted';
            empty.textContent = 'No comments yet. Leave the first thought.';
            list.append(empty);
        }
    }

    function resetCommentForm(commentForm) {
        commentForm.reset();
        commentForm.dataset.parentId = '';
        commentForm.dataset.editId = '';
        commentForm.elements.author.readOnly = false;
        commentForm.elements.author.value = session.author;
        commentForm.querySelector('.studio-reply-status').hidden = true;
        commentForm.querySelector('[type=submit]').textContent = 'Comment';
    }

    async function connect() {
        try {
            session = await request('/api/session');
            const { moments: records } = await request('/api/moments');
            for (const moment of records) moments.set(moment.id, moment);
            connection.textContent = 'Saved on this Mac · ' + records.length + (records.length === 1 ? ' moment' : ' moments');
            newButton.disabled = false;
            for (const row of rows) {
                const moment = moments.get(row.dataset.momentId);
                if (!moment) continue;
                socialForRow(row, moment.social);
                row.querySelector('.studio-edit')?.addEventListener('click', async () => {
                    try { openComposer(await request(`/api/moments/${moment.id}`)); } catch (error) { say(error.message); }
                });
                row.querySelector('.studio-like')?.addEventListener('click', async event => {
                    const button = event.currentTarget;
                    button.disabled = true;
                    try {
                        moment.social = await request(`/api/moments/${moment.id}/like`, { method: 'POST', body: { liked: button.getAttribute('aria-pressed') !== 'true' } });
                        socialForRow(row, moment.social);
                    } catch (error) { say(error.message); }
                    finally { button.disabled = false; }
                });
                row.querySelector('.studio-comments-toggle')?.addEventListener('click', event => {
                    const panel = row.querySelector('.studio-comments');
                    panel.hidden = !panel.hidden;
                    event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
                    if (!panel.hidden) panel.querySelector('textarea').focus();
                });
                row.querySelector('.studio-comment-form')?.addEventListener('submit', async event => {
                    event.preventDefault();
                    const commentForm = event.currentTarget;
                    const button = commentForm.querySelector('[type=submit]');
                    const status = commentForm.querySelector('.studio-form-message');
                    button.disabled = true;
                    status.textContent = 'Saving…';
                    try {
                        const editId = commentForm.dataset.editId;
                        const path = `/api/moments/${moment.id}/comments` + (editId ? `/${editId}` : '');
                        moment.social = await request(path, { method: editId ? 'PUT' : 'POST', body: {
                            text: commentForm.elements.text.value, author: commentForm.elements.author.value,
                            parent_id: commentForm.dataset.parentId || null
                        } });
                        socialForRow(row, moment.social);
                        resetCommentForm(commentForm);
                        status.textContent = 'Saved';
                    } catch (error) { status.textContent = error.message; }
                    finally { button.disabled = false; }
                });
            }
        } catch (error) {
            connection.textContent = 'Start the Moments app to post, edit, and comment.';
            for (const row of rows) for (const button of row.querySelectorAll('.studio-actions button')) button.disabled = true;
        }
    }

    newButton.addEventListener('click', () => openComposer());
    document.querySelector('#studio-close').addEventListener('click', closeComposer);
    composer.addEventListener('cancel', event => { event.preventDefault(); closeComposer(); });
    form.addEventListener('input', () => { dirty = true; });
    form.elements.date.addEventListener('input', saveLocation);
    document.querySelector('#studio-files').addEventListener('change', event => { addPhotos(event.target.files); event.target.value = ''; });
    const dropzone = document.querySelector('.studio-upload');
    dropzone.addEventListener('dragover', event => { event.preventDefault(); dropzone.classList.add('studio-upload-active'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('studio-upload-active'));
    dropzone.addEventListener('drop', event => { event.preventDefault(); dropzone.classList.remove('studio-upload-active'); addPhotos(event.dataTransfer.files); });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (submitting) return;
        submitting = true;
        for (const button of form.querySelectorAll('[type=submit]')) button.disabled = true;
        message.textContent = 'Saving your moment…';
        try {
            const payload = {
                body: form.elements.body.value, date: form.elements.date.value + ':00+08:00',
                tags: form.elements.tags.value.split(',').map(tag => tag.trim()).filter(Boolean),
                note: form.elements.note.value, link: form.elements.link.value,
                link_text: form.elements.link_text.value, top: Number(form.elements.top.value || 0),
                pictures: retainedPhotos, draft: event.submitter?.value === 'draft', revision: editing?.revision
            };
            const data = new FormData();
            data.append('post', JSON.stringify(payload));
            for (const upload of uploads) data.append('pictures', upload.file);
            const path = '/api/moments' + (editing ? `/${editing.id}` : '');
            const result = await request(path, { method: editing ? 'PUT' : 'POST', body: data });
            dirty = false;
            if (result.build_error) {
                editing = result.moment;
                retainedPhotos = [...editing.pictures];
                for (const upload of uploads) URL.revokeObjectURL(upload.url);
                uploads = [];
                renderPhotos();
                message.textContent = `Your moment is saved, but its preview needs a correction: ${result.build_error}`;
                document.querySelector('#studio-publish').textContent = 'Save changes';
            } else location.assign('/');
        } catch (error) { message.textContent = error.message; }
        finally {
            submitting = false;
            for (const button of form.querySelectorAll('[type=submit]')) button.disabled = false;
        }
    });

    window.addEventListener('beforeunload', event => {
        const unfinishedComment = rows.some(row => row.querySelector('.studio-comment-form textarea')?.value.trim());
        if (dirty || submitting || unfinishedComment) { event.preventDefault(); event.returnValue = ''; }
    });
    setInterval(async () => {
        if (!session || composer.open || rows.some(row => row.querySelector('.studio-comment-form textarea')?.value.trim())) return;
        try {
            const current = await request('/api/session');
            if (current.generation !== session.generation && !current.build_error) location.reload();
        } catch (_) { /* Keep unsaved text intact when the editor is offline. */ }
    }, 4000);
    connect();
})();
