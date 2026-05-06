/* ═══════════════════════════════════════════════
   AI Assistant
   ═══════════════════════════════════════════════ */
$('chat-btn').addEventListener('click', async () => {
  const input = $('chat-input');
  const query = input.value.trim();
  if (!query || !state.selectedCampusId) return;

  const history = $('chat-history');

  // Remove the placeholder text if it's the first message
  if (history.children.length === 1 && history.children[0].style.fontStyle === 'italic') {
    history.innerHTML = '';
  }

  // 1. Display User Message
  const userDiv = document.createElement('div');
  userDiv.className = 'chat-msg user';
  userDiv.textContent = query;
  history.appendChild(userDiv);

  // 2. Lock input and show loading state
  input.value = '';
  input.disabled = true;
  $('chat-btn').disabled = true;
  $('chat-btn').textContent = 'Thinking...';
  history.scrollTop = history.scrollHeight;

  try {
    // 3. Call your new backend endpoint
    const res = await api('/assistant/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_query: query, campus_id: state.selectedCampusId })
    });

    // 4. Display Bot Message
    const botDiv = document.createElement('div');
    botDiv.className = 'chat-msg bot';

    // Parse line breaks for formatting
    let contentHtml = `<div>${res.answer.replace(/\n/g, '<br>')}</div>`;

    // Append the source citations if the backend found any
    if (res.sources && res.sources.length > 0) {
      // Deduplicate sources just in case
      const uniqueSources = [...new Set(res.sources)];
      contentHtml += `<div class="chat-sources">Sources: ${uniqueSources.join(', ')}</div>`;
    }

    botDiv.innerHTML = contentHtml;
    history.appendChild(botDiv);

  } catch (e) {
    // Display errors directly in the chat
    const errDiv = document.createElement('div');
    errDiv.className = 'chat-msg bot';
    errDiv.style.color = '#d9534f';
    errDiv.textContent = 'Error: ' + e.message;
    history.appendChild(errDiv);
  } finally {
    // 5. Unlock input
    input.disabled = false;
    $('chat-btn').disabled = false;
    $('chat-btn').textContent = 'Ask';
    history.scrollTop = history.scrollHeight;
    input.focus();
  }
});

// Trigger the "Ask" button when the user presses Enter
$('chat-input').addEventListener('keypress', e => {
  if (e.key === 'Enter') $('chat-btn').click();
});
